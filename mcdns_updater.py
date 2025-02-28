import functools
import ipaddress
import sys

from cloudflare import Cloudflare
from cloudflare.types.dns import record_list_params, srv_record_param
from mcstatus import JavaServer
from pydantic import BaseModel, field_validator


class Node(BaseModel):
    subdomain: str

    host: str
    port: int = 25565

    bandwidth: int

    @field_validator("host", mode="after")
    @classmethod
    def check_host(cls, val: str) -> str:
        try:
            ipaddress.ip_address(val)
        except ValueError:
            return val

        assert False, "IP address is not allowed for SRV record."


class Config(BaseModel):
    api_token: str
    zone_id: str
    subdomain: str

    nodes: list[Node]
    timeout: float = 5.0

    @field_validator("nodes", mode="after")
    @classmethod
    def check_nodes(cls, val: list[Node]) -> list[Node]:
        subdomains = [node.subdomain for node in val]
        assert len(set(subdomains)) == len(subdomains), "conflict subdomains found."
        return val


def concat_domain(*parts: str) -> str:
    return ".".join(part.removesuffix(".") for part in parts)


def read_config() -> Config:
    with open("config.json", "r", encoding="utf-8") as fp:
        content = fp.read()

    return Config.model_validate_json(content)


def check_preference(node: Node, timeout: float) -> float:
    server = JavaServer(node.host, node.port, timeout)

    try:
        lat = server.ping()  # pyright: ignore[reportUnknownMemberType]
    except Exception as ex:
        eprint("ping failed:", ex)
        return 0

    return (node.bandwidth**2) / lat


def update_record(
    cloudflare: Cloudflare, zone_id: str, subdomain: str, host: str, port: int
) -> None:
    zone = cloudflare.zones.get(zone_id=zone_id)
    assert zone, "got none when get zone info."

    fqdn = concat_domain("_minecraft._tcp", subdomain, zone.name)

    print(f"updating {fqdn} SRV record to {host}:{port}...")

    records = cloudflare.dns.records.list(
        zone_id=zone_id,
        name=record_list_params.Name(exact=fqdn),
        type="SRV",
        per_page=1,
    )

    if records.result:
        commit_dns_record = functools.partial(
            cloudflare.dns.records.update, dns_record_id=records.result[0].id
        )
    else:
        commit_dns_record = cloudflare.dns.records.create

    data = srv_record_param.Data(target=host, port=port, priority=0, weight=0)
    commit_dns_record(zone_id=zone_id, name=fqdn, data=data, type="SRV")

    print(f"{fqdn} SRV record updated.")


eprint = functools.partial(print, file=sys.stderr)


def main() -> None:
    config = read_config()

    prefs = [(node, check_preference(node, config.timeout)) for node in config.nodes]
    print("\n".join(f"tested: {node=}, {pref=}" for node, pref in prefs))

    selected, pref = max(prefs, key=lambda val: val[1])

    if pref == 0:
        return eprint("no node available.")

    print(f"selected: {selected!r}")

    with Cloudflare(api_token=config.api_token) as cloudflare:
        update = functools.partial(update_record, cloudflare, config.zone_id)

        for node in config.nodes:
            subdomain = concat_domain(node.subdomain, config.subdomain)
            update(subdomain, node.host, node.port)

        update(config.subdomain, selected.host, selected.port)


if __name__ == "__main__":
    main()
