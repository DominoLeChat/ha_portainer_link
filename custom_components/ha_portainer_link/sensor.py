import logging
import hashlib
from homeassistant.helpers.entity import Entity
from homeassistant.const import STATE_UNKNOWN
from homeassistant.helpers import entity_registry as er
from .const import DOMAIN
from .portainer_api import PortainerAPI

_LOGGER = logging.getLogger(__name__)

_LOGGER.info("Loaded Portainer sensor integration.")

def _build_stable_unique_id(entry_id, endpoint_id, container_name, stack_info, suffix):
    if stack_info.get("is_stack_container"):
        stack_name = stack_info.get("stack_name", "unknown")
        service_name = stack_info.get("service_name", container_name)
        base = f"{stack_name}_{service_name}"
    else:
        base = container_name
    
    sanitized = base.replace('-', '_').replace(' ', '_').replace('/', '_')
    return f"entry_{entry_id}_endpoint_{endpoint_id}_{sanitized}_{suffix}"

def _get_host_display_name(base_url):
    """Extract a clean host name from the base URL for display purposes."""
    host = base_url.replace("https://", "http://").replace("http://", "")
    host = host.rstrip("/")
    for port in [":9000", ":9443", ":80", ":443"]:
        if host.endswith(port):
            host = host[:-len(port)]
    
    if host.replace('.', '').replace('-', '').replace('_', '').isdigit():
        return host
    else:
        parts = host.split('.')
        if len(parts) >= 2:
            return parts[0]
        else:
            return host

def _get_host_hash(base_url):
    """Generate a short hash of the host URL for unique identification."""
    return hashlib.md5(base_url.encode()).hexdigest()[:8]

async def async_setup_entry(hass, entry, async_add_entities):
    config = entry.data
    host = config["host"]
    username = config.get("username")
    password = config.get("password")
    api_key = config.get("api_key")
    endpoint_id = config["endpoint_id"]
    entry_id = entry.entry_id

    _LOGGER.info("🚀 Setting up HA Portainer Link sensors for entry %s (endpoint %s)", entry_id, endpoint_id)
    
    host_display_name = _get_host_display_name(host)
    _LOGGER.info("🏷️ Extracted host display name: %s", host_display_name)

    api = PortainerAPI(host, username, password, api_key)
    await api.initialize()

    containers = await api.get_containers(endpoint_id)
    _LOGGER.info("📦 Found %d containers to process", len(containers))

    entities = []
    stack_containers_count = 0
    standalone_containers_count = 0

    # Migrate existing entities to stable unique_ids
    try:
        er_registry = er.async_get(hass)
        for container in containers:
            name = container.get("Names", ["unknown"])[0].strip("/")
            container_id = container["Id"]
            container_info = await api.inspect_container(endpoint_id, container_id)
            stack_info = api.get_container_stack_info(container_info) if container_info else {"is_stack_container": False}
            
            # Removed version sensors from suffixes list
            suffixes = [
                ("status", "sensor"),
                ("cpu_usage", "sensor"),
                ("memory_usage", "sensor"),
                ("uptime", "sensor"),
                ("image", "sensor"),
            ]
            
            for suffix, domain_name in suffixes:
                old_uid = f"entry_{entry_id}_endpoint_{endpoint_id}_{container_id}_{suffix}"
                new_uid = _build_stable_unique_id(entry_id, endpoint_id, name, stack_info, suffix)
                
                if old_uid == new_uid:
                    continue
                    
                ent_id = er_registry.async_get_entity_id(domain_name, DOMAIN, old_uid)
                if ent_id:
                    try:
                        er_registry.async_update_entity(ent_id, new_unique_id=new_uid)
                        _LOGGER.debug("Migrated %s unique_id: %s -> %s", ent_id, old_uid, new_uid)
                    except Exception as e:
                        _LOGGER.debug("Could not migrate %s: %s", ent_id, e)

    except Exception as e:
        _LOGGER.debug("Entity registry migration skipped/failed: %s", e)

    for container in containers:
        name = container.get("Names", ["unknown"])[0].strip("/")
        container_id = container["Id"]
        state = container.get("State", STATE_UNKNOWN)
        
        _LOGGER.debug("🔍 Processing container: %s (ID: %s, State: %s)", name, container_id, state)
        
        container_info = await api.inspect_container(endpoint_id, container_id)
        stack_info = api.get_container_stack_info(container_info) if container_info else {"is_stack_container": False}
        
        if stack_info.get("is_stack_container"):
            stack_containers_count += 1
        else:
            standalone_containers_count += 1

        entities.append(ContainerStatusSensor(name, state, api, endpoint_id, container_id, stack_info, entry_id))
        entities.append(ContainerCPUSensor(name, api, endpoint_id, container_id, stack_info, entry_id))
        entities.append(ContainerMemorySensor(name, api, endpoint_id, container_id, stack_info, entry_id))
        entities.append(ContainerUptimeSensor(name, api, endpoint_id, container_id, stack_info, entry_id))
        entities.append(ContainerImageSensor(name, container, api, endpoint_id, container_id, stack_info, entry_id))
        # Version sensors removed here

    _LOGGER.info("✅ Created %d entities (%d stack containers, %d standalone containers)", 
                 len(entities), stack_containers_count, standalone_containers_count)
    
    async_add_entities(entities, update_before_add=True)


class BaseContainerSensor(Entity):
    """Base class for all container sensors."""
    def __init__(self, container_name, container_id, api, endpoint_id, stack_info, entry_id):
        self._container_name = container_name
        self._container_id = container_id
        self._api = api
        self._endpoint_id = endpoint_id
        self._stack_info = stack_info
        self._entry_id = entry_id

    async def _find_current_container_id(self):
        try:
            containers = await self._api.get_containers(self._endpoint_id)
            if not containers:
                return None
                
            if self._stack_info.get("is_stack_container"):
                expected_stack = self._stack_info.get("stack_name")
                expected_service = self._stack_info.get("service_name")
                
                for container in containers:
                    labels = container.get("Labels", {}) or {}
                    if (labels.get("com.docker.compose.project") == expected_stack and 
                        labels.get("com.docker.compose.service") == expected_service):
                        return container.get("Id")
            
            for container in containers:
                names = container.get("Names", []) or []
                if not names:
                    continue
                name = names[0].strip("/")
                if name == self._container_name:
                    return container.get("Id")
            
            return None
        except Exception:
            return None

    async def _ensure_container_bound(self) -> None:
        try:
            info = await self._api.get_container_info(self._endpoint_id, self._container_id)
            if not info or not isinstance(info, dict) or not info.get("Id"):
                new_id = await self._find_current_container_id()
                if new_id and new_id != self._container_id:
                    self._container_id = new_id
        except Exception:
            new_id = await self._find_current_container_id()
            if new_id and new_id != self._container_id:
                self._container_id = new_id

    @property
    def device_info(self):
        host_name = _get_host_display_name(self._api.base_url)
        host_hash = _get_host_hash(self._api.base_url)
        
        if self._stack_info.get("is_stack_container"):
            stack_name = self._stack_info.get("stack_name", "unknown_stack")
            device_id = f"entry_{self._entry_id}_endpoint_{self._endpoint_id}_stack_{stack_name}_{host_hash}_{host_name.replace('.', '_').replace(':', '_')}"
            return {
                "identifiers": {(DOMAIN, device_id)},
                "name": f"Stack: {stack_name} ({host_name})",
                "manufacturer": "Docker via Portainer",
                "model": "Docker Stack",
                "configuration_url": f"{self._api.base_url}/#!/stacks/{stack_name}",
            }
        else:
            device_id = f"entry_{self._entry_id}_endpoint_{self._endpoint_id}_container_{self._container_id}_{host_hash}_{host_name.replace('.', '_').replace(':', '_')}"
            return {
                "identifiers": {(DOMAIN, device_id)},
                "name": f"{self._container_name} ({host_name})",
                "manufacturer": "Docker via Portainer",
                "model": "Docker Container",
                "configuration_url": f"{self._api.base_url}/#!/containers/{self._container_id}/details",
            }

class ContainerStatusSensor(BaseContainerSensor):
    def __init__(self, name, state, api, endpoint_id, container_id, stack_info, entry_id):
        super().__init__(name, container_id, api, endpoint_id, stack_info, entry_id)
        self._attr_name = f"{name} Status"
        self._attr_unique_id = _build_stable_unique_id(entry_id, endpoint_id, name, stack_info, "status")
        self._state = state

    @property
    def state(self):
        return self._state or STATE_UNKNOWN

    @property
    def icon(self):
        return {
            "running": "mdi:docker",
            "exited": "mdi:close-circle",
            "paused": "mdi:pause-circle",
        }.get(self._state, "mdi:help-circle")

    async def async_update(self):
        try:
            await self._ensure_container_bound()
            container_info = await self._api.get_container_info(self._endpoint_id, self._container_id)
            if container_info:
                self._state = container_info.get("State", {}).get("Status", STATE_UNKNOWN)
            else:
                self._state = STATE_UNKNOWN
        except Exception as e:
            _LOGGER.warning("Failed to get status for %s: %s", self._attr_name, e)
            self._state = STATE_UNKNOWN

class ContainerCPUSensor(BaseContainerSensor):
    def __init__(self, name, api, endpoint_id, container_id, stack_info, entry_id):
        super().__init__(name, container_id, api, endpoint_id, stack_info, entry_id)
        self._attr_name = f"{name} CPU Usage"
        self._attr_unique_id = _build_stable_unique_id(entry_id, endpoint_id, name, stack_info, "cpu_usage")
        self._state = STATE_UNKNOWN

    @property
    def state(self):
        return self._state

    @property
    def unit_of_measurement(self):
        return "%"

    @property
    def icon(self):
        return "mdi:cpu-64-bit"

    async def async_update(self):
        await self._ensure_container_bound()
        try:
            stats = await self._api.get_container_stats(self._endpoint_id, self._container_id)
            cpu_usage = stats["cpu_stats"]["cpu_usage"]["total_usage"]
            precpu_usage = stats["precpu_stats"]["cpu_usage"]["total_usage"]
            system_cpu = stats["cpu_stats"]["system_cpu_usage"]
            pre_system_cpu = stats["precpu_stats"]["system_cpu_usage"]
            
            cpu_delta = cpu_usage - precpu_usage
            system_delta = system_cpu - pre_system_cpu
            cpu_count = stats.get("cpu_stats", {}).get("online_cpus", 1)

            usage = (cpu_delta / system_delta) * cpu_count * 100.0 if system_delta > 0 else 0
            self._state = round(usage, 2)
        except Exception:
            self._state = STATE_UNKNOWN

class ContainerMemorySensor(BaseContainerSensor):
    def __init__(self, name, api, endpoint_id, container_id, stack_info, entry_id):
        super().__init__(name, container_id, api, endpoint_id, stack_info, entry_id)
        self._attr_name = f"{name} Memory Usage"
        self._attr_unique_id = _build_stable_unique_id(entry_id, endpoint_id, name, stack_info, "memory_usage")
        self._state = STATE_UNKNOWN

    @property
    def state(self):
        return self._state

    @property
    def unit_of_measurement(self):
        return "MB"

    @property
    def icon(self):
        return "mdi:memory"

    async def async_update(self):
        await self._ensure_container_bound()
        try:
            stats = await self._api.get_container_stats(self._endpoint_id, self._container_id)
            mem_bytes = stats["memory_stats"]["usage"]
            self._state = round(mem_bytes / (1024 * 1024), 2)
        except Exception:
            self._state = STATE_UNKNOWN

class ContainerUptimeSensor(BaseContainerSensor):
    def __init__(self, name, api, endpoint_id, container_id, stack_info, entry_id):
        super().__init__(name, container_id, api, endpoint_id, stack_info, entry_id)
        self._attr_name = f"{name} Uptime"
        self._attr_unique_id = _build_stable_unique_id(entry_id, endpoint_id, name, stack_info, "uptime")
        self._state = STATE_UNKNOWN

    @property
    def state(self):
        return self._state

    @property
    def icon(self):
        return "mdi:clock-outline"

    async def async_update(self):
        await self._ensure_container_bound()
        try:
            container_info = await self._api.get_container_info(self._endpoint_id, self._container_id)
            started_at = container_info["State"]["StartedAt"]
            
            if started_at and started_at != "0001-01-01T00:00:00Z":
                from datetime import datetime, timezone
                dt = datetime.fromisoformat(started_at.replace('Z', '+00:00'))
                now = datetime.now(timezone.utc)
                diff = now - dt.replace(tzinfo=timezone.utc)
                
                if diff.days > 0:
                    self._state = f"{diff.days} days ago"
                elif diff.seconds > 3600:
                    self._state = f"{diff.seconds // 3600} hours ago"
                elif diff.seconds > 60:
                    self._state = f"{diff.seconds // 60} minutes ago"
                else:
                    self._state = "Just started"
            else:
                self._state = "Not started"
        except Exception:
            self._state = STATE_UNKNOWN

class ContainerImageSensor(BaseContainerSensor):
    def __init__(self, name, container_data, api, endpoint_id, container_id, stack_info, entry_id):
        super().__init__(name, container_id, api, endpoint_id, stack_info, entry_id)
        self._attr_name = f"{name} Image"
        self._attr_unique_id = _build_stable_unique_id(entry_id, endpoint_id, name, stack_info, "image")
        self._state = container_data.get("Image", STATE_UNKNOWN)

    @property
    def state(self):
        return self._state

    @property
    def icon(self):
        return "mdi:docker"

    async def async_update(self):
        try:
            await self._ensure_container_bound()
            container_info = await self._api.get_container_info(self._endpoint_id, self._container_id)
            if container_info:
                self._state = container_info.get("Config", {}).get("Image", STATE_UNKNOWN)
            else:
                self._state = STATE_UNKNOWN
        except Exception:
            self._state = STATE_UNKNOWN
