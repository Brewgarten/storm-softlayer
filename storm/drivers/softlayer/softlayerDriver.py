"""
`libcloud` driver for the FYRE infrastructure
"""
import ConfigParser
import copy
import datetime
import logging
import os
import string
import time

import SoftLayer
from libcloud.compute.base import (Node, NodeDriver, NodeImage, NodeLocation, NodeSize, NodeState)
from libcloud.compute.providers import set_driver


log = logging.getLogger(__name__)

DEFAULT_CPU_SIZE = 2
DEFAULT_RAM_SIZE = 2048
DEFAULT_DISK_SIZE = 10

HARDWARE_INFO_ITEMS = [
    "activeComponents", # include hardware components such as disks
    "bareMetalInstanceFlag", # include whether this is bare metal
    "billingItem.orderItem.order.userRecord[username]",
    "datacenter",
    "domain",
    "fullyQualifiedDomainName",
    "globalIdentifier",
    "hardwareStatusId",
    "hostname",
    "id",
    "memoryCapacity",
    "operatingSystem.passwords", # include passwords
    "primaryBackendIpAddress",
    "primaryIpAddress",
    "processorPhysicalCoreAmount",
    "tagReferences"
]

IMAGE_INFO_ITEMS = [
    "accountId",
    "blockDevices,"
    "createDate",
    "globalIdentifier",
    "id",
    "name",
    "parentId"
]

LOCATION_INFO_ITEMS = [
    "id",
    "locationAddress",
    "longName",
    "name"
]

VIRTUAL_INFO_ITEMS = [
    "activeTransaction.transactionStatus[friendlyName,name]",
    "billingItem.orderItem.order.userRecord[username]",
    "blockDevices.diskImage", # include block devices
    "datacenter",
    "domain",
    "fullyQualifiedDomainName",
    "globalIdentifier",
    "hostname",
    "id",
    "lastKnownPowerState.name",
    "maxCpu",
    "maxMemory",
    "operatingSystem.passwords",
    "powerState",
    "primaryBackendIpAddress",
    "primaryIpAddress",
    "status",
    "tagReferences"
]

class SoftLayerCluster(object):
    """
    A SoftLayer cluster that containes references to the nodes
    contained in the cluster

    :param name: name
    :type name: str
    :param driver: SoftLayer node driver
    :type driver: :class:`~SoftLayerNodeDriver`
    """
    def __init__(self, name, driver):
        self.name = name
        self.driver = driver
        self.nodes = {}

    def __repr__(self):
        return "<Cluster: name={name}, nodes={nodes}, driver={driver} ...>".format(
            name=self.name,
            nodes=self.nodes.keys(),
            driver=self.driver
        )

    def destroy(self, timeout=600):
        """
        Destroy the nodes in the cluster as well as the cluster itself

        :param timeout: timeout in seconds
        :type timeout: int
        """
        start = datetime.datetime.utcnow()
        remainingNodes = []
        for name, node in self.nodes.items():
            if not self.driver.destroy_node(node):
                remainingNodes.append(name)
        if not remainingNodes:
            end = datetime.datetime.utcnow()
            log.info("Destroying cluster '%s' took '%s'", self.name, end-start)
        else:
            log.error("Could not destroy cluster '%s' because nodes '%s' could not be destroyed",
                      self.name,
                      ",".join(remainingNodes))

class SoftLayerNodeLocation(NodeLocation):
    """
    A physical location where nodes can be

    :param locationId: location id
    :type locationId: str
    :param name: name
    :type name: str
    :param country: country
    :type country: str
    :param driver: driver
    :type driver: :class:`~libcloud.compute.base.NodeDriver`
    :param extra: optional provider specific attributes
    :type extra: dict
    """
    def __init__(self, locationId, name, country, driver, extra=None):
        super(SoftLayerNodeLocation, self).__init__(locationId, name, country, driver)
        self.extra = extra or {}

class SoftLayerNodeDriver(NodeDriver):
    """
    SoftLayer node driver using the SoftLayer Python API

    :param username: user name
    :type username: str
    :param apiKey: api key
    :type apiKey: str
    """
    NODE_STATE_MAP = {
        "RUNNING": NodeState.RUNNING,
        "HALTED": NodeState.STOPPED,
        "PAUSED": NodeState.UNKNOWN,
        "INITIATING": NodeState.PENDING
    }

    features = {"create_node": ["generates_password"]}
    name = "SoftLayerNodeDriver"
    type = "sl"

    def __init__(self, username, apiKey):
        super(SoftLayerNodeDriver, self).__init__(username, apiKey)
        self.client = SoftLayer.create_client_from_env(username=username, api_key=apiKey)

    def _hardware_to_node(self, instance):
        """
        Convert a SoftLayer hardware instance dictionary into a node

        :param instance: instance
        :type instance: dict
        :returns: node
        :rtype: :class:`~libcloud.compute.base.Node`
        """
        publicIps = []
        if "primaryIpAddress" in instance:
            publicIps.append(instance["primaryIpAddress"])
        privateIps = []
        if "primaryBackendIpAddress" in instance:
            privateIps.append(instance["primaryBackendIpAddress"])

        disks = []
        for activeComponent in instance.get("activeComponents", []):
            hardwareComponentType = activeComponent["hardwareComponentModel"]["hardwareGenericComponentModel"]["hardwareComponentType"]
            if hardwareComponentType["keyName"] == "HARD_DRIVE":
                # note that the default unit is GB
                disks.append(int(activeComponent["hardwareComponentModel"]["capacity"]))

        sizeExtra = {
            "cpu": int(instance["processorPhysicalCoreAmount"]),
            "disks" : disks,
            "memory": 1024 * int(instance["memoryCapacity"])
        }
        size = SoftLayerNodeSize(self, extra=sizeExtra)

        extra = {
            "domain": instance["domain"],
            "hostname": instance["hostname"],
            "tags": [
                reference["tag"]["name"]
                for reference in instance.get("tagReferences", [])
                if reference["tag"]["internal"] == 0 # non-zero indicates internal
            ],
            "type": "bare_metal" if instance["bareMetalInstanceFlag"] == 1 else "virtual_server"
        }
        try:
            extra["password"] = instance["operatingSystem"]["passwords"][0]["password"]
        except:
            extra["password"] = "unknown"

        # TODO: check power state
        state = NodeState.RUNNING

        return Node(
            instance["id"],
            instance["fullyQualifiedDomainName"],
            state,
            publicIps,
            privateIps,
            self,
            size=size,
            extra=extra
        )

    def _virtual_to_node(self, instance):
        """
        Convert a SoftLayer instance dictionary into a node

        :param instance: instance
        :type instance: dict
        :returns: node
        :rtype: :class:`~libcloud.compute.base.Node`
        """
        publicIps = []
        if "primaryIpAddress" in instance:
            publicIps.append(instance["primaryIpAddress"])
        privateIps = []
        if "primaryBackendIpAddress" in instance:
            privateIps.append(instance["primaryBackendIpAddress"])

        disks = []
        for blockDevice in instance.get("blockDevices", []):
            # add all non-swap disks
            if blockDevice["mountType"] == "Disk" and not blockDevice["diskImage"]["description"].endswith("-SWAP"):
                # note that the default unit is GB
                disks.append(int(blockDevice["diskImage"]["capacity"]))

        sizeExtra = {
            "cpu": int(instance["maxCpu"]),
            "disks" : disks,
            "memory": int(instance["maxMemory"])
        }
        size = SoftLayerNodeSize(self, extra=sizeExtra)

        extra = {
            "domain": instance["domain"],
            "hostname": instance["hostname"],
            "tags": [
                reference["tag"]["name"]
                for reference in instance.get("tagReferences", [])
                if reference["tag"]["internal"] == 0 # non-zero indicates internal
            ],
            "type": "virtual_server"
        }
        if "powerState" in instance and "keyName" in instance["powerState"]:
            state = self.NODE_STATE_MAP.get(instance["powerState"]["keyName"], NodeState.UNKNOWN)
        else:
            state = NodeState.UNKNOWN

        try:
            extra["password"] = instance["operatingSystem"]["passwords"][0]["password"]
        except:
            extra["password"] = "unknown"

        return Node(
            instance["id"],
            instance["fullyQualifiedDomainName"],
            state,
            publicIps,
            privateIps,
            self,
            size=size,
            extra=extra
        )

    def create_node(self, timeout=600, **kwargs):
        """
        Create a new node instance. This instance will be started
        automatically.

        :param image: image/template to be used for the node
        :type image: :class:`~libcloud.compute.base.NodeImage`
        :param location: location
        :type location: :class:`~libcloud.compute.base.NodeLocation`
        :param name: node name
        :type name: str
        :param size: size
        :type size: :class:`~libcloud.compute.base.NodeSize`
        :param timeout: timeout in seconds for the node to be provisioned
        :type timeout: int
        :returns: node
        :rtype: :class:`~libcloud.compute.base.Node`
        """
        if "image" not in kwargs:
            raise ValueError("'image' needs to be specified")
        image = kwargs["image"]
        if "location" not in kwargs:
            raise ValueError("'location' needs to be specified")
        location = kwargs["location"]
        if "name" not in kwargs:
            raise ValueError("'name' needs to be specified")
        name = kwargs["name"]
        if "size" not in kwargs:
            raise ValueError("'size' needs to be specified")
        size = kwargs["size"]

        log.info("Creating node '%s' using '%s'", name, image.name)
        start = datetime.datetime.utcnow()

        createOptions = {
            "cpus": size.cpu,
            "datacenter": location.id,
            "domain": "sl-cloud.com", # TODO: check if needed
            "hostname": name,
            "hourly": True,
            "memory": size.ram,
            "nic_speed": size.bandwidth
        }

        if isinstance(image, SoftLayerOperatingSystemImage):
            createOptions["os_code"] = image.id
            createOptions["disks"] = size.diskCapacities
            createOptions["local_disk"] = len(size.diskCapacities) <= 2  # if there are >2 disks, then choose SAN
        else:
            createOptions["image_id"] = image.id
            # TODO: it ssems like we do not need the disks but need to select the local_disk flag correctly based on how many disks are in the image
            createOptions["disks"] = size.diskCapacities
            createOptions["local_disk"] = len(size.diskCapacities) <= 2  # if there are >2 disks, then choose SAN

        nodes = self.ex_create_nodes([createOptions], timeout=timeout)
        if nodes:
            end = datetime.datetime.utcnow()
            log.info("Creating node '%s' took '%s'", name, end-start)
            return nodes[0]
        else:
            log.error("Could not create node '%s' timed out", name)
            return None

    def destroy_node(self, node):
        """
        Destroy a node.

        :param node: The node to be destroyed
        :type node: :class:`.Node`
        :return: True if the destroy was successful, False otherwise.
        :rtype: ``bool``
        """
        vs = SoftLayer.VSManager(self.client)
        return vs.cancel_instance(int(node.id))

    def ex_get_available_cpus(self):
        """
        Get information on the available cpu options

        :returns: list of cpu options
        :rtype: [int]
        """
        vs = SoftLayer.VSManager(self.client)
        options = vs.get_create_options()
        cpus = set([
            int(item["template"]["startCpus"])
            for item in options["processors"]
        ])
        return sorted(cpus)

    def ex_get_available_disk_capacities(self):
        """
        Get information on the available disk capacity options for each
        disk number

        :returns: disk number to capacity lists mappings
        :rtype: dict
        """
        vs = SoftLayer.VSManager(self.client)
        options = vs.get_create_options()
        capacities = {}
        for item in options["blockDevices"]:
            blockDevice = item["template"]["blockDevices"][0]
            number = int(blockDevice["device"])
            # deal with fact that labeling uses 0 for the first disk
            if number == 0:
                number = 1
            if number not in capacities:
                capacities[number] = set()
            capacities[number].add(blockDevice["diskImage"]["capacity"])
        return capacities

    def ex_get_available_ram(self):
        """
        Get information on the available ram/memory options

        :returns: list of ram/memory options
        :rtype: [int]
        """
        vs = SoftLayer.VSManager(self.client)
        options = vs.get_create_options()
        return sorted([
            int(item["template"]["maxMemory"])
            for item in options["memory"]
        ])

    def ex_get_available_operating_systems(self):
        """
        Get information on the available operating systems

        :returns: operating system code to name mapping
        :rtype: dict
        """
        vs = SoftLayer.VSManager(self.client)
        options = vs.get_create_options()
        return {
            item["template"]["operatingSystemReferenceCode"]: item["itemPrice"]["item"]["description"]
            for item in options["operatingSystems"]
        }

    def ex_get_cluster_by_name(self, name):
        """
        Get a cluster by name

        :param name: name
        :type name: str
        :returns: cluster
        :rtype: :class:`~SoftLayerCluster`
        """
        nameTag = "storm.cluster.name:{0}".format(name)
        nodes = []
        nodes.extend(self.ex_get_hardware_nodes(tags=[nameTag]))
        nodes.extend(self.ex_get_virtual_nodes(tags=[nameTag]))
        if nodes:
            cluster = SoftLayerCluster(name, self)
            cluster.nodes = {
                node.name: node
                for node in nodes
            }
            return cluster
        return None

    def ex_create_cluster(self, timeout=600, **kwargs):
        """
        Create a new cluster. All nodes in this cluster will be started
        automatically.

        :param cluster: cluster name
        :type cluster: str
        :param image: image to be used for the nodes
        :type image: :class:`~libcloud.compute.base.NodeImage`
        :param location: location
        :type location: :class:`~libcloud.compute.base.NodeLocation`
        :param names: node names
        :type names: [str]
        :param size: size
        :type size: :class:`~libcloud.compute.base.NodeSize`
        :param timeout: timeout in seconds for the nodes to be provisioned
        :type timeout: int
        :returns: cluster
        :rtype: :class:`~SoftLayerCluster`
        """
        if "cluster" not in kwargs:
            raise ValueError("'cluster' needs to be specified")
        cluster = kwargs["cluster"]
        if "image" not in kwargs:
            raise ValueError("'image' needs to be specified")
        image = kwargs["image"]
        if "location" not in kwargs:
            raise ValueError("'location' needs to be specified")
        location = kwargs["location"]
        if "names" not in kwargs:
            raise ValueError("'names' needs to be specified")
        names = kwargs["names"]
        if "size" not in kwargs:
            raise ValueError("'size' needs to be specified")
        size = kwargs["size"]

        # check for nodes that already exist
        softlayerCluster = self.ex_get_cluster_by_name(cluster)
        if softlayerCluster:
            log.warn("Cluster with name '%s' already exists", cluster)
            existingNodes = [
                name
                for name in names
                if name in softlayerCluster.nodes
            ]
            if existingNodes:
                log.error("Nodes '%s' already exists in cluster '%s'", ",".join(existingNodes), cluster)
                return None

        log.info("Creating cluster '%s' with nodes '%s' using '%s'", cluster, ",".join(names), image.name)
        start = datetime.datetime.utcnow()

        createOptions = {
            "cpus": size.cpu,
            "datacenter": location.id,
            "domain": "sl-cloud.com", # TODO: check if needed
            "hourly": True,
            "memory": size.ram,
            "nic_speed": size.bandwidth
        }

        if isinstance(image, SoftLayerOperatingSystemImage):
            createOptions["os_code"] = image.id
            createOptions["disks"] = size.diskCapacities
            createOptions["local_disk"] = len(size.diskCapacities) <= 2  # if there are >2 disks, then choose SAN
        else:
            createOptions["image_id"] = image.id
            # TODO: it ssems like we do not need the disks but need to select the local_disk flag correctly based on how many disks are in the image
            createOptions["disks"] = size.diskCapacities
            createOptions["local_disk"] = len(size.diskCapacities) <= 2  # if there are >2 disks, then choose SAN

        publicVlans = self.ex_get_vlans(includePrivate=False, includePublic=True, datacenter=location.id)
        if publicVlans:
            # sort by the vlans with the largest number of guests
            publicVlans = sorted(publicVlans, key=lambda vlan: vlan.get("virtualGuestCount", 0))
            largestPublicVlan = publicVlans.pop()
            log.info("Using public vlan '%s' with currently '%d' guests",
                     largestPublicVlan["id"], largestPublicVlan.get("virtualGuestCount", 0))
            createOptions["public_vlan"] = largestPublicVlan["id"]
        privateVlans = self.ex_get_vlans(includePrivate=True, includePublic=False, datacenter=location.id)
        if privateVlans:
            # sort by the vlans with the largest number of guests
            privateVlans = sorted(privateVlans, key=lambda vlan: vlan.get("virtualGuestCount", 0))
            largestPrivateVlan = privateVlans.pop()
            log.info("Using private vlan '%s' with currently '%d' guests",
                     largestPrivateVlan["id"], largestPrivateVlan.get("virtualGuestCount", 0))
            createOptions["private_vlan"] = largestPrivateVlan["id"]

        configurations = []
        for name in names:
            nodeConfig = createOptions.copy()
            nodeConfig["hostname"] = "{cluster}-{name}".format(cluster=cluster, name=name)
            nodeConfig["tags"] = "storm.cluster,storm.cluster.name:{cluster}".format(cluster=cluster)
            configurations.append(nodeConfig)

        nodes = self.ex_create_nodes(configurations, timeout=timeout)
        if nodes:
            newCluster = SoftLayerCluster(cluster, self)
            newCluster.nodes = {
                node.name: node
                for node in nodes
            }
            end = datetime.datetime.utcnow()
            log.info("Creating cluster '%s' with nodes '%s' took '%s'", cluster, ",".join(names), end-start)
            return newCluster
        else:
            log.error("Creating cluster '%s' with nodes '%s' timed out", cluster, ",".join(names))
            return None

    def ex_create_nodes(self, configs, timeout=600):
        """
        Create several instances

        :param configs: list of configurations
        :type configs: [dict]
        :param timeout: timeout in seconds for the nodes to be provisioned
        :type timeout: int
        """
        totalStart = datetime.datetime.utcnow()
        vs = SoftLayer.VSManager(self.client)

        # extract tags
        tags = [
            config.pop("tags", None)
            for config in configs
        ]

        objects = []
        for config in configs:
            template = vs._generate_create_dict(**config)
            if "image_id" in config:
                # work around for 'Invalid value provided for 'blockDevices'. Block devices may not be provided when using an image template.' issue
                # see https://github.com/softlayer/softlayer-python/issues/325
                template.pop("blockDevices", None)
                template["imageTemplateId"] = config["image_id"]
            objects.append(template)

        instanceInfos = vs.guest.createObjects(objects)

        # set tags on the created instances
        for instance, tag in zip(instanceInfos, tags):
            if tag:
                vs.guest.setTags(tag, id=instance["id"])

        nodes = []
        transactions = {}
        readyInstances = set()
        while timeout > 0:

            # go through all the nodes and check their current transactions
            for instanceInfo in instanceInfos:

                if instanceInfo["fullyQualifiedDomainName"] not in readyInstances:

                    instance = vs.get_instance(instanceInfo["id"])
                    activeTransactionId = SoftLayer.utils.lookup(instance, "activeTransaction", "id")
                    activeTransactionName = SoftLayer.utils.lookup(instance, "activeTransaction", "transactionStatus", "friendlyName")

                    # log if the transaction has changed
                    if (activeTransactionName is not None
                            and activeTransactionName != transactions.get(instanceInfo["fullyQualifiedDomainName"], None)):
                        log.info("%s: %s", instanceInfo["fullyQualifiedDomainName"], activeTransactionName)
                        transactions[instanceInfo["fullyQualifiedDomainName"]] = activeTransactionName

                    # check that the instance has finished provisioning
                    if all([instance.get('provisionDate'),
                            not activeTransactionId]):
                        readyInstances.add(instance["fullyQualifiedDomainName"])

            if len(readyInstances) == len(instanceInfos):
                break
            else:
                timeout -= 1
                time.sleep(1)

        if len(readyInstances) != len(instanceInfos):
            log.info("Creating %d nodes timed out", len(instanceInfos))
            return nodes

        for instanceInfo in instanceInfos:
            # make sure we include masks for information we need
            virtualkwargs = {"mask" : "mask[{0}]".format(",".join(VIRTUAL_INFO_ITEMS))}
            instance = vs.get_instance(instanceInfo['id'], **virtualkwargs)
            nodes.append(self._virtual_to_node(instance))

        totalEnd = datetime.datetime.utcnow()
        log.info("Creating %d nodes took %s", len(instanceInfos), totalEnd-totalStart)

        return nodes

    @staticmethod
    def ex_from_config(configFileName="~/.softlayer"):
        """
        Get SoftLayer node driver based on settings in the specified config file

        :returns: SoftLayer node driver
        :rtype: :class:`~SoftLayerNodeDriver`
        """
        config = ConfigParser.ConfigParser()
        config.read(os.path.expanduser(configFileName))
        return SoftLayerNodeDriver(
            config.get("softlayer", "username"),
            config.get("softlayer", "api_key")
        )

    def ex_get_hardware_nodes(self, **kwargs):
        """
        Get a list of hardware nodes (server and bare metal), optionally filtered by specified keyword arguments

        :param boolean hourly: include hourly instances
        :param boolean monthly: include monthly instances
        :param list tags: filter based on list of tags
        :param integer cpus: filter based on number of CPUS
        :param integer memory: filter based on amount of memory
        :param string hostname: filter based on hostname
        :param string domain: filter based on domain
        :param string local_disk: filter based on local_disk
        :param string datacenter: filter based on datacenter
        :param integer nic_speed: filter based on network speed (in MBPS)
        :param string public_ip: filter based on public ip address
        :param string private_ip: filter based on private ip address
        :returns: list of nodes
        :rtype: [:class:`~libcloud.compute.base.Node`]
        """
        nodes = []
        # make sure we include masks for information we need
        serverItems = [
            'activeTransaction[id, transactionStatus[friendlyName,name]]',
        ]
        kwargs["mask"] = "[mask[{info}],mask(SoftLayer_Hardware_Server)[{server}]]".format(
            info=",".join(HARDWARE_INFO_ITEMS),
            server=",".join(serverItems))
        hardwareManager = SoftLayer.HardwareManager(self.client)
        for hardware in hardwareManager.list_hardware(**kwargs):
            nodes.append(self._hardware_to_node(hardware))
        nodes = sorted(nodes, key=lambda node: node.name)
        return nodes

    def ex_get_image_by_name(self, name):
        """
        Get a image by name

        :param name: name
        :type name: str
        :returns: :class:`~libcloud.compute.base.NodeImage`
        """
        for image in self.list_images():
            if image.name == name:
                return image
        return None

    def ex_get_location_by_name(self, name):
        """
        Get a location by name

        :param name: name
        :type name: str
        :returns: :class:`~libcloud.compute.base.NodeLocation`
        """
        for location in self.list_locations():
            if location.name == name:
                return location
        return None

    def ex_get_size_by_attributes(self, cpus, ram, disks):
        """
        Get size by attributes

        :param cpus: cpus
        :type cpus: int
        :param ram: ram in MB
        :type ram: int
        :param disks: list of disks capacities in GB
        :type disks: list
        :returns: node size
        :rtype: :class:`~FyreNodeSize`
        """
        availableCpus = self.ex_get_available_cpus()
        if cpus not in availableCpus:
            log.error("'%d' number of cpus is not supported (available '%s')",
                      cpus,
                      ",".join(str(cpu) for cpu in availableCpus))
            return None
        availableRam = self.ex_get_available_ram()
        if ram not in availableRam:
            log.error("'%d' amount of ram is not supported (available '%s')",
                      ram,
                      ",".join(str(ram) for ram in availableRam))
            return None
        availableCapacities = self.ex_get_available_disk_capacities()
        for number, diskCapacity in enumerate(disks, 1):
            if diskCapacity not in availableCapacities[number]:
                log.error("'%d' size of disk is not supported for disk number '%d' (available '%s')",
                          diskCapacity,
                          number,
                          ",".join(str(capacity) for capacity in availableCapacities[number]))
                return None
        return SoftLayerNodeSize(self, extra={
            "cpu": cpus,
            "disks" : disks,
            "memory": ram,
            "type": "virtual_server"
        })

    def ex_get_virtual_nodes(self, **kwargs):
        """
        Get a list of virtual nodes, optionally filtered by specified keyword arguments

        :param boolean hourly: include hourly instances
        :param boolean monthly: include monthly instances
        :param list tags: filter based on list of tags
        :param integer cpus: filter based on number of CPUS
        :param integer memory: filter based on amount of memory
        :param string hostname: filter based on hostname
        :param string domain: filter based on domain
        :param string local_disk: filter based on local_disk
        :param string datacenter: filter based on datacenter
        :param integer nic_speed: filter based on network speed (in MBPS)
        :param string public_ip: filter based on public ip address
        :param string private_ip: filter based on private ip address
        :returns: list of nodes
        :rtype: [:class:`~libcloud.compute.base.Node`]
        """
        nodes = []
        vs = SoftLayer.VSManager(self.client)
        # make sure we include masks for information we need
        kwargs["mask"] = "mask[{0}]".format(",".join(VIRTUAL_INFO_ITEMS))
        for instance in vs.list_instances(**kwargs):
            nodes.append(self._virtual_to_node(instance))
        nodes = sorted(nodes, key=lambda node: node.name)
        return nodes

    def ex_get_vlans(self, includePrivate=True, includePublic=True, datacenter=None):
        """
        Get a list of vlans

        :param includePrivate: include private vlans
        :type includePrivate: bool
        :param includePublic: include public vlans
        :type includePublic: bool
        :param datacenter: datacenter
        :type datacenter: str
        :returns: list of vlan information dictionaries
        :rtype: [dict]
        """
        if not includePrivate and not includePublic:
            return []
        networkManager = SoftLayer.NetworkManager(self.client)

        vlans = networkManager.list_vlans(datacenter=datacenter)
        privateVlans = []
        publicVlans = []
        for vlan in vlans:
            subnetIdentifiers = [subnet["networkIdentifier"]
                                 for subnet in vlan.get("subnets", [])]
            if all([identifier.startswith("10.") for identifier in subnetIdentifiers]):
                privateVlans.append(vlan)
            else:
                publicVlans.append(vlan)

        if includePrivate and not includePublic:
            return privateVlans
        elif includePublic and not includePrivate:
            return publicVlans
        else:
            return vlans

    def ex_list_clusters(self):
        """
        Get a list of clusters

        :returns: [:class:`~SoftLayerCluster`]
        """
        clusterMap = {}
        nodes = []
        nodes.extend(self.ex_get_hardware_nodes(tags=["storm.cluster"]))
        nodes.extend(self.ex_get_virtual_nodes(tags=["storm.cluster"]))
        for node in nodes:
            clusterName = get_cluster_name(node.extra["tags"])
            if clusterName not in clusterMap:
                clusterMap[clusterName] = SoftLayerCluster(clusterName, self)
            clusterMap[clusterName].nodes[node.name] = node
        clusters = sorted(clusterMap.values(), key=lambda cluster: cluster.name)
        return clusters

    def get_image(self, image_id):
        """
        Returns a single node image from a provider.

        :param image_id: Node to run the task on.
        :type image_id: ``str``

        :rtype :class:`.NodeImage`:
        :return: NodeImage instance on success.
        """
        for image in self.list_images():
            if image.id == image_id:
                return image
        return None

    def list_images(self, location=None):
        """
        Get a list of images

        :param location: location
        :type location: :class:`~libcloud.compute.base.NodeLocation`
        :returns: [:class:`~libcloud.compute.base.NodeImage`]
        """
        # TODO: incorporate location
        # include operating system images
        images = [
            SoftLayerOperatingSystemImage(osCode, osName, self)
            for osCode, osName in self.ex_get_available_operating_systems().items()
        ]
        # include private and public images
        imageManager = SoftLayer.ImageManager(self.client)

        # make sure we include masks for information we need
        kwargs = {"mask": "mask[{0}]".format(",".join(IMAGE_INFO_ITEMS))}
        softlayerImages = imageManager.list_private_images(**kwargs)
        softlayerImages.extend(imageManager.list_public_images(**kwargs))
        softlayerImages = sorted(softlayerImages, key=lambda image: image["name"])
        for image in softlayerImages:
            extra = {
                "id": image["id"]
            }
            printableCharacters = [
                char
                for char in image["name"]
                if char in string.printable
            ]
            sanitizedName = "".join(printableCharacters).strip()
            images.append(
                NodeImage(image["globalIdentifier"],
                          sanitizedName,
                          self,
                          extra)
            )
        return images

    def list_locations(self):
        """
        List data centers for a provider

        :return: list of node location objects
        :rtype: ``list`` of :class:`.NodeLocation`
        """
        locations = []
        mask = {"mask" : "mask[{0}]".format(",".join(LOCATION_INFO_ITEMS))}
        datacenters = self.client["Location"].getDatacenters(**mask)
        for datacenter in datacenters:
            extra = {
                "id": datacenter["id"]
            }
            country = ""
            if "locationAddress" in datacenter and isinstance(datacenter["locationAddress"], dict):
                address = datacenter["locationAddress"]
                country = address["country"]
                extra["address"] = address["address1"]
                if "address2" in address:
                    extra["address"] += ", {0}".format(address["address2"])
                extra["city"] = address["city"]
                extra["description"] = address["description"]
            locations.append(
                SoftLayerNodeLocation(datacenter["name"],
                                      datacenter["longName"],
                                      country,
                                      self,
                                      extra=extra)
            )
        return locations

    def list_nodes(self):
        """
        Get a list of nodes

        :returns: [:class:`~libcloud.compute.base.Node`]
        """
        nodes = []
        nodes.extend(self.ex_get_hardware_nodes())
        nodes.extend(self.ex_get_virtual_nodes())
        nodes = sorted(nodes, key=lambda node: node.name)
        return nodes

    def list_sizes(self, location=None):
        """
        List sizes on a provider

        :param location: The location at which to list sizes
        :type location: :class:`.NodeLocation`

        :return: list of node size objects
        :rtype: ``list`` of :class:`.NodeSize`
        """
        sizes = [
            SoftLayerNodeSize(None, extra={
                "cpu": DEFAULT_CPU_SIZE,
                "disks" : [100],
                "memory": DEFAULT_RAM_SIZE/1024,
                "type": "virtual_server"
            })
        ]
        vs = SoftLayer.VSManager(self.client)
        # add different component sizes
        virtualMachineOptions = vs.get_create_options()

        # TODO: determine need to support dedicatedAccountHostOnlyFlag
        cpus = set([
            int(item["template"]["startCpus"])
            for item in virtualMachineOptions["processors"]
        ])
        sizes = get_additional_sizes(
            "cpu",
            sorted(cpus),
            sizes
        )
        memory = [
            int(item["template"]["maxMemory"])
            for item in virtualMachineOptions["memory"]
        ]
        sizes = get_additional_sizes(
            "memory",
            memory,
            sizes
        )

        # adjust driver properties
        for size in sizes:
            size.driver = self
        return sizes

class SoftLayerNodeSize(NodeSize):
    """
    A node image size information

    :param driver: driver
    :type driver: :class:`~libcloud.compute.base.NodeDriver`
    :param extra: optional provider specific attributes
    :type extra: dict
    """
    def __init__(self, driver, extra=None):
        super(SoftLayerNodeSize, self).__init__(
            0, "n/a", 0, 100, 1000, 0, driver, extra)

    @property
    def bandwidth(self):
        """
        Amount of bandwidth in Mbps
        """
        return self.extra.get("bandwidth", 1000)

    @bandwidth.setter
    def bandwidth(self, value):
        # we do not need store the bandwidth since we auto generate it from the extra properties
        pass

    def bandwidthType(self):
        """
        Type of bandwidth
        """
        return self.extra.get("bandwidthType", "Public & Private Network Uplinks")

    @property
    def cpu(self):
        """
        Number of CPUs
        """
        return self.extra.get("cpu", 0)

    @property
    def disk(self):
        """
        Amount of disk storage in GB
        """
        return sum(self.diskCapacities)

    @disk.setter
    def disk(self, value):
        # we do not need store the disk since we auto generate it from the extra properties
        pass

    @property
    def diskType(self):
        """
        Disk type
        """
        if "localDiskFlag" in self.extra:
            local = self.extra["localDiskFlag"]
        else:
            local = len(self.diskCapacities) <= 2
        return "LOCAL" if local else "SAN"

    @property
    def diskCapacities(self):
        """
        List of disk capacities in GB
        """
        return self.extra.get("disks", [100])

    @property
    def id(self):
        """
        Unique id
        """
        return "{cpu}-cpu-{ram}-ram-{diskCapacities}-disks".format(
            cpu=self.cpu,
            ram=self.ram,
            diskCapacities=self.diskCapacities
        )

    @id.setter
    def id(self, value):
        # we do not need store the id since we auto generate it from the extra properties
        pass

    @property
    def name(self):
        """
        Human readable name
        """
        return "{cpu}xCPU, {ram}GB, {disks} {diskType} disks ({diskCapacities}), {bandwidth}Mbps".format(
            cpu=self.cpu,
            ram=self.ram/1024,
            disks=len(self.diskCapacities),
            diskType=self.diskType,
            diskCapacities=",".join(str(capacity) for capacity in self.diskCapacities),
            bandwidth=self.bandwidth
        )

    @name.setter
    def name(self, value):
        # we do not need store the name since we auto generate it from the extra properties
        pass

    @property
    def ram(self):
        """
        Amount of memory in MB
        """
        return self.extra.get("memory", 0)

    @ram.setter
    def ram(self, value):
        # we do not need store the ram since we auto generate it from the extra properties
        pass

class SoftLayerOperatingSystemImage(NodeImage):
    """
    An operating system image

    :param code: operating system code
    :type code: str
    :param name: name
    :type name: str
    :param driver: driver
    :type driver: :class:`~libcloud.compute.base.NodeDriver`
    """
    def __init__(self, code, name, driver):
        super(SoftLayerOperatingSystemImage, self).__init__(code, name, driver)

def get_additional_sizes(name, options, existingSizes):
    """
    Get a combination of existing sizes with the specified options

    :param option: option name
    :type option: str
    :param options: configuration options
    :type options: list
    :param existingSizes: existing sizes
    :type existingSizes: [:class:`FyreNodeSize`]
    :returns: list of new node sizes
    :rtype: [:class:`FyreNodeSize`]
    """
    newSizes = []
    for option in options:
        for size in existingSizes:
            newSize = copy.deepcopy(size)
            newSize.extra.update({name:option})
            newSizes.append(newSize)
    return newSizes

def get_cluster_name(tags):
    """
    Get the cluster name from the list of specified tags

    :param tags: tags
    :type tags: [str]
    :returns: cluster name
    :rtype: str
    """
    for tag in tags:
        if tag.startswith("storm.cluster.name:"):
            return tag.replace("storm.cluster.name:", "")
    return None

def slcli():
    """
    Pass-through to SoftLayer commandline client
    """
    import SoftLayer.CLI.core
    SoftLayer.CLI.core.main()

set_driver(SoftLayerNodeDriver.type, SoftLayerNodeDriver.__module__, SoftLayerNodeDriver.name)

if __name__ == '__main__':
    slcli()
