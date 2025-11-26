import asyncio
import os
import logging
import time
import httpx
from jinja2 import Environment, FileSystemLoader
from kubernetes import client, config as k8s_config

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("controller")

INTERVAL = int(os.getenv("INTERVAL", "10"))
TIMEOUT = int(os.getenv("TIMEOUT", "2"))
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
TEMPLATE_DIR = os.path.join(BASE_DIR, "templates")
COREFILE_PATH = "/etc/coredns/Corefile"
ZONEFILE_DIR = "/etc/coredns/zones"
GEOIP_DBPATH = "/data/GeoLite2-City.mmdb"
GEOIP_ENABLED = os.getenv("GEOIP", "false").lower() == "true"

env = Environment(loader=FileSystemLoader(TEMPLATE_DIR))
corefile_template = env.get_template("corefile.j2")
zonefile_template = env.get_template("zonefile.j2")


try:
    k8s_config.load_incluster_config()
except Exception:
    try:
        k8s_config.load_kube_config()
    except Exception:
        logger.warning("No K8s config found, CRD features will fail.")


custom_api = client.CustomObjectsApi()


async def get_domain_configs():
    domain_map = {}
    try:
        ret = custom_api.list_cluster_custom_object(
            group="cyberun.cloud", version="v1", plural="gslbconfigs"
        )
        for item in ret.get("items", []):
            spec = item.get("spec", {})
            domain = spec.get("domain")
            nameservers = spec.get("nameservers")
            if not domain or not nameservers:
                continue

            if domain not in domain_map:
                domain_map[domain] = {
                    "nameservers": nameservers,
                    "raw_records": [],
                }
            records = spec.get("records", [])
            domain_map[domain]["raw_records"].extend(records)
    except Exception as e:
        logger.error(f"Failed to fetch CRDs: {e}")
    return domain_map


async def check_tcp(address: str, port: int) -> bool:
    try:
        _, writer = await asyncio.wait_for(
            asyncio.open_connection(address, port), timeout=TIMEOUT
        )
        writer.close()
        await writer.wait_closed()
        return True
    except Exception:
        return False


async def check_http(
    client: httpx.AsyncClient, address: str, port: int, path: str, scheme: str = "http"
) -> bool:
    url = f"{scheme}://{address}:{port}{path}"
    try:
        resp = await client.get(url, timeout=TIMEOUT, follow_redirects=True)
        return 200 <= resp.status_code < 300
    except Exception:
        return False


async def verify_target(client: httpx.AsyncClient, target: dict) -> bool:
    address = target.get("address")
    if not address:
        return False

    protocol = target.get("protocol", "http").lower()
    port = target.get("port", 80)
    path = target.get("path", "/")

    is_healthy = False
    if protocol == "tcp":
        is_healthy = await check_tcp(address, port)
    elif protocol in ["http", "https"]:
        is_healthy = await check_http(client, address, port, path, scheme=protocol)
    else:
        logger.warning(f"Unknown protocol: {protocol}, marking as unhealthy.")

    if not is_healthy:
        loc = target.get("location", "default")
        logger.warning(f"Unhealthy: {protocol}://{address}:{port}{path} (Loc: {loc})")

    return is_healthy


async def resolve_healthy_records(client: httpx.AsyncClient, raw_records: list) -> dict:
    healthy_map = {}
    check_tasks = []
    metadata_list = []

    for rec in raw_records:
        name = rec.get("name")
        if not name:
            continue
        if name not in healthy_map:
            healthy_map[name] = []

        candidates = rec.get("targets", [])
        for target in candidates:
            check_tasks.append(verify_target(client, target))
            metadata_list.append((name, target))

    if not check_tasks:
        return healthy_map

    results = await asyncio.gather(*check_tasks)

    for (name, target), is_healthy in zip(metadata_list, results):
        if is_healthy:
            healthy_map[name].append(target)

    return healthy_map


def organize_data_by_region(healthy_map: dict) -> dict:
    zone_views = {"default": {}}

    all_regions = set()
    for rec_name, targets in healthy_map.items():
        for t in targets:
            loc = t.get("location")
            if loc:
                all_regions.add(loc.upper())

    for rec_name, targets in healthy_map.items():
        default_targets = []
        for t in targets:
            if not t.get("location"):
                weight = t.get("weight", 1)
                for _ in range(weight):
                    default_targets.append(t)

        if default_targets:
            zone_views["default"][rec_name] = default_targets

    for region in all_regions:
        zone_views[region] = {}

        for rec_name, targets in healthy_map.items():
            region_targets = []

            for t in targets:
                if t.get("location", "").upper() == region:
                    weight = t.get("weight", 1)
                    for _ in range(weight):
                        region_targets.append(t)

            if not region_targets:
                if rec_name in zone_views["default"]:
                    region_targets = zone_views["default"][rec_name]

            if region_targets:
                zone_views[region][rec_name] = region_targets

    return zone_views


async def update_corefile(domain_meta: dict):
    try:
        content = corefile_template.render(
            geoip_enabled=GEOIP_ENABLED,
            geoip_dbpath=GEOIP_DBPATH,
            domain_meta=domain_meta,
        )

        current = ""
        if os.path.exists(COREFILE_PATH):
            with open(COREFILE_PATH, "r") as f:
                current = f.read()

        if content != current:
            with open(COREFILE_PATH, "w") as f:
                f.write(content)
            logger.info("Corefile updated.")
    except Exception as e:
        logger.error(f"Failed to update Corefile: {e}")


async def run_loop():
    logger.info("Starting SimpleGSLB Controller ...")
    os.makedirs(ZONEFILE_DIR, exist_ok=True)

    async with httpx.AsyncClient(verify=False) as client:
        while True:
            start_time = asyncio.get_event_loop().time()

            try:
                domains_config = await get_domain_configs()
                serial = int(time.time())

                current_domain_meta = {}

                for domain, data in domains_config.items():
                    try:
                        nameservers = data.get("nameservers", [])
                        raw_recs = data.get("raw_records", [])

                        if not nameservers:
                            logger.warning(
                                f"Domain {domain} has no nameservers, skipping."
                            )
                            continue

                        healthy_map = await resolve_healthy_records(client, raw_recs)

                        views_data = organize_data_by_region(healthy_map)

                        active_regions = [
                            r for r in views_data.keys() if r != "default"
                        ]
                        current_domain_meta[domain] = sorted(active_regions)

                        for view_name, records_in_view in views_data.items():
                            filename = f"db.{domain}.{view_name}"
                            try:
                                content = zonefile_template.render(
                                    domain=domain,
                                    nameservers=nameservers,
                                    serial=serial,
                                    records=records_in_view,
                                )

                                zone_path = os.path.join(ZONEFILE_DIR, filename)
                                temp_path = zone_path + ".tmp"

                                with open(temp_path, "w") as f:
                                    f.write(content)
                                os.rename(temp_path, zone_path)
                            except Exception as e:
                                logger.error(
                                    f"Failed to write zone file {filename}: {e}"
                                )

                    except Exception as domain_e:
                        logger.error(f"Error processing domain {domain}: {domain_e}")

                await update_corefile(current_domain_meta)

            except Exception as e:
                logger.error(f"Error in run loop: {e}")

            elapsed = asyncio.get_event_loop().time() - start_time
            await asyncio.sleep(max(0, INTERVAL - elapsed))


def run():
    asyncio.run(run_loop())
