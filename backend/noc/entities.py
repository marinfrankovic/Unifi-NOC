import re


_DEVICE_ENTITY = re.compile(r"(dev|port|radio):([0-9a-f]{2}(?::[0-9a-f]{2}){5})(?::([^:]+))?", re.IGNORECASE)


def entity_device_mac(entity_id: str) -> str | None:
    match = _DEVICE_ENTITY.fullmatch(entity_id)
    if not match:
        return None
    kind, mac, component = match.groups()
    if (kind.lower() == "dev") != (component is None):
        return None
    return mac.lower()