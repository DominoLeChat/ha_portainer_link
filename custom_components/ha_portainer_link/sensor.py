import logging
import hashlib

from homeassistant.helpers.update_coordinator import CoordinatorEntity
from homeassistant.components.sensor import SensorEntity
from homeassistant.const import STATE_UNKNOWN

from .const import DOMAIN

_LOGGER = logging.getLogger(__name__)

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
    return hashlib.md5(base_url.encode()).hexdigest()[:8]

async def async_setup_entry(hass, entry, async_add_entities):
    """Set up Portainer sensors using the coordinator."""
    coordinator = hass.data[DOMAIN][entry.entry_id]
    config = entry.data
    endpoint_id = config["endpoint_id"]
    entry_id = entry.entry_id

    # On attend que le coordinateur ait récupéré les premières données
    # pour être sûr d'avoir la liste des conteneurs
    if not coordinator.data:
        await coordinator.async_config_entry_first_refresh()

    containers = coordinator.data.get("containers", {})
    _LOGGER.info("📦 Sensor setup: Found %d containers in coordinator data", len(containers))

    entities = []
    
    # Pour chaque conteneur trouvé dans le coordinateur
    for container_id, container_data in containers.items():
        name = container_data.get("Names", ["unknown"])[0].strip("/")
        
        # On récupère les infos de stack déjà calculées par le coordinateur
        # (plus besoin de refaire un appel API inspect_container ici !)
        stack_info = coordinator.container_stack_info.get(container_id, {"is_stack_container": False})

        # Création des entités liées au coordinateur
        entities.append(ContainerStatusSensor(coordinator, name, endpoint_id, container_id, stack_info, entry_id))
        entities.append(ContainerCPUSensor(coordinator, name, endpoint_id, container_id, stack_info, entry_id))
        entities.append(ContainerMemorySensor(coordinator, name, endpoint_id, container_id, stack_info, entry_id))
        entities.append(ContainerUptimeSensor(coordinator, name, endpoint_id, container_id, stack_info, entry_id))
        entities.append(ContainerImageSensor(coordinator, name, endpoint_id, container_id, stack_info, entry_id))

    async_add_entities(entities)


class BaseContainerSensor(CoordinatorEntity, SensorEntity):
    """Base class for all container sensors that follows the coordinator."""
    
    def __init__(self, coordinator, container_name, endpoint_id, container_id, stack_info, entry_id):
        super().__init__(coordinator)
        self._container_name = container_name
        self._container_id = container_id
        self._endpoint_id = endpoint_id
        self._stack_info = stack_info
        self._entry_id = entry_id
        self._api_url = coordinator.api.base_url

    @property
    def device_info(self):
        host_name = _get_host_display_name(self._api_url)
        host_hash = _get_host_hash(self._api_url)
        
        if self._stack_info.get("is_stack_container"):
            stack_name = self._stack_info.get("stack_name", "unknown_stack")
            device_id = f"entry_{self._entry_id}_endpoint_{self._endpoint_id}_stack_{stack_name}_{host_hash}_{host_name.replace('.', '_').replace(':', '_')}"
            return {
                "identifiers": {(DOMAIN, device_id)},
                "name": f"Stack: {stack_name} ({host_name})",
                "manufacturer": "Docker via Portainer",
                "model": "Docker Stack",
                "configuration_url": f"{self._api_url}/#!/stacks/{stack_name}",
            }
        else:
            device_id = f"entry_{self._entry_id}_endpoint_{self._endpoint_id}_container_{self._container_id}_{host_hash}_{host_name.replace('.', '_').replace(':', '_')}"
            return {
                "identifiers": {(DOMAIN, device_id)},
                "name": f"{self._container_name} ({host_name})",
                "manufacturer": "Docker via Portainer",
                "model": "Docker Container",
                "configuration_url": f"{self._api_url}/#!/containers/{self._container_id}/details",
            }
            
    @property
    def available(self) -> bool:
        """Entity is available if coordinator is successful and container is in the list."""
        return (
            super().available 
            and "containers" in self.coordinator.data 
            and self._container_id in self.coordinator.data["containers"]
        )

class ContainerStatusSensor(BaseContainerSensor):
    def __init__(self, coordinator, name, endpoint_id, container_id, stack_info, entry_id):
        super().__init__(coordinator, name, endpoint_id, container_id, stack_info, entry_id)
        self._attr_name = f"{name} Status"
        self._attr_unique_id = _build_stable_unique_id(entry_id, endpoint_id, name, stack_info, "status")

    @property
    def native_value(self):
        container = self.coordinator.data["containers"].get(self._container_id, {})
        # Gestion robuste des formats d'état (dict ou str)
        state_obj = container.get("State", {})
        if isinstance(state_obj, dict):
            return state_obj.get("Status", STATE_UNKNOWN)
        return STATE_UNKNOWN

    @property
    def icon(self):
        val = self.native_value
        return {
            "running": "mdi:docker",
            "exited": "mdi:close-circle",
            "paused": "mdi:pause-circle",
        }.get(val, "mdi:help-circle")

class ContainerCPUSensor(BaseContainerSensor):
    def __init__(self, coordinator, name, endpoint_id, container_id, stack_info, entry_id):
        super().__init__(coordinator, name, endpoint_id, container_id, stack_info, entry_id)
        self._attr_name = f"{name} CPU Usage"
        self._attr_unique_id = _build_stable_unique_id(entry_id, endpoint_id, name, stack_info, "cpu_usage")
        self._attr_native_unit_of_measurement = "%"
        self._attr_icon = "mdi:cpu-64-bit"

    @property
    def native_value(self):
        # Lecture directe depuis les métriques calculées par le coordinateur
        metrics = self.coordinator.metrics.get(self._container_id, {})
        return metrics.get("cpu_percent", STATE_UNKNOWN)

class ContainerMemorySensor(BaseContainerSensor):
    def __init__(self, coordinator, name, endpoint_id, container_id, stack_info, entry_id):
        super().__init__(coordinator, name, endpoint_id, container_id, stack_info, entry_id)
        self._attr_name = f"{name} Memory Usage"
        self._attr_unique_id = _build_stable_unique_id(entry_id, endpoint_id, name, stack_info, "memory_usage")
        self._attr_native_unit_of_measurement = "MB"
        self._attr_icon = "mdi:memory"

    @property
    def native_value(self):
        metrics = self.coordinator.metrics.get(self._container_id, {})
        return metrics.get("memory_mb", STATE_UNKNOWN)

class ContainerUptimeSensor(BaseContainerSensor):
    def __init__(self, coordinator, name, endpoint_id, container_id, stack_info, entry_id):
        super().__init__(coordinator, name, endpoint_id, container_id, stack_info, entry_id)
        self._attr_name = f"{name} Uptime"
        self._attr_unique_id = _build_stable_unique_id(entry_id, endpoint_id, name, stack_info, "uptime")
        self._attr_icon = "mdi:clock-outline"

    @property
    def native_value(self):
        metrics = self.coordinator.metrics.get(self._container_id, {})
        uptime_s = metrics.get("uptime_s")
        
        if uptime_s is None:
            return "Not started"
            
        # Conversion simple en texte lisible
        if uptime_s > 86400:
            return f"{uptime_s // 86400} days ago"
        elif uptime_s > 3600:
            return f"{uptime_s // 3600} hours ago"
        elif uptime_s > 60:
            return f"{uptime_s // 60} minutes ago"
        else:
            return "Just started"

class ContainerImageSensor(BaseContainerSensor):
    def __init__(self, coordinator, name, endpoint_id, container_id, stack_info, entry_id):
        super().__init__(coordinator, name, endpoint_id, container_id, stack_info, entry_id)
        self._attr_name = f"{name} Image"
        self._attr_unique_id = _build_stable_unique_id(entry_id, endpoint_id, name, stack_info, "image")
        self._attr_icon = "mdi:docker"

    @property
    def native_value(self):
        container = self.coordinator.data["containers"].get(self._container_id, {})
        # On préfère l'info de config si dispo, sinon l'image ID brute
        return container.get("Image", STATE_UNKNOWN)
