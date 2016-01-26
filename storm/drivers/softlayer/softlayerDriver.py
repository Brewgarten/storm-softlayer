import copy
import datetime
import logging
import time
import traceback

log = logging.getLogger(__name__)

from libcloud.compute.providers import set_driver
from libcloud.compute.base import NodeDriver, Node, NodeImage, NodeLocation, NodeSize, StorageVolume
from libcloud.compute.base import NodeState

import SoftLayer

NODE_STATE_MAP = {
    'RUNNING': NodeState.RUNNING,
    'HALTED': NodeState.STOPPED,
    'PAUSED': NodeState.UNKNOWN,
    'INITIATING': NodeState.PENDING
}

DEFAULT_CPU_SIZE = 2
DEFAULT_RAM_SIZE = 2048
DEFAULT_DISK_SIZE = 10

VIRTUAL_INFO_ITEMS = [
    'activeTransaction.transactionStatus[friendlyName,name]',
    'billingItem.orderItem.order.userRecord[username]',
    'blockDevices.diskImage', # include block devices
    'datacenter',
    'domain',
    'fullyQualifiedDomainName',
    'globalIdentifier',
    'hostname',
    'id',
    'lastKnownPowerState.name',
    'maxCpu',
    'maxMemory',
    'operatingSystem.passwords',
    'powerState',
    'primaryBackendIpAddress',
    'primaryIpAddress',
    'status'
]

class SoftLayerPythonAPINodeLocation(NodeLocation):
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
        super(SoftLayerPythonAPINodeLocation, self).__init__(
             locationId, name, country, driver)
        self.extra = extra or {}

class SoftLayerPythonAPINodeSize(NodeSize):
    """
    A node image size information

    :param sizeId: size id
    :type sizeId: str
    :param name: name
    :type name: str
    :param ram: amount of memory (in MB) provided by this size
    :type ram: int
    :param disk: amount of disk storage (in GB) provided by this image
    :type disk: int
    :param bandwidth: amount of bandiwdth included with this size
    :type bandwidth: int
    :param price: price (in US dollars) of running this node for an hour
    :type price: float
    :param driver: driver
    :type driver: :class:`~libcloud.compute.base.NodeDriver`
    :param extra: optional provider specific attributes
    :type extra: dict
    """
    def __init__(self, sizeId, name, ram, disk, bandwidth, price, driver, extra=None):
        super(SoftLayerPythonAPINodeSize, self).__init__(
            sizeId, name, ram, disk, bandwidth, price, driver, extra)

    @property
    def bandwidth(self):
        return self.extra.get("networkComponents", [{"maxSpeed": 0}])[0]["maxSpeed"]

    @bandwidth.setter
    def bandwidth(self, value):
        # we do not need store the bandwidth since we auto generate it from the extra properties
        pass

    @property
    def bandwidthType(self):
        return "Private Network Uplink" if self.extra.get("privateNetworkOnlyFlag", False) else "Public & Private Network Uplinks"

    @property
    def blockDevices(self):
        blockDevices = self.extra.get("blockDevices", [])
        return sorted(blockDevices, key=lambda blockDevice: blockDevice["device"])

    @property
    def cpu(self):
        return self.extra.get("startCpus", 0)

    @property
    def cpuType(self):
        return "Private" if self.extra.get("dedicatedAccountHostOnlyFlag", False) else ""

    @property
    def disk(self):
        return sum(self.diskCapacities)

    @disk.setter
    def disk(self, value):
        # we do not need store the disk since we auto generate it from the extra properties
        pass

    @property
    def diskType(self):
        return "LOCAL" if self.extra.get("localDiskFlag", True) else "SAN"

    @property
    def diskCapacities(self):
        return [blockDevice["diskImage"]["capacity"] for blockDevice in self.blockDevices]

    @property
    def name(self):
        return "{cpu}x2.0GHz {cpuType}, {ram}GB, {disks} {diskType} disks ({diskCapacities}), {bandwidth}Mb {bandwidthType}".format(
            cpuType = self.cpuType,
            cpu = self.cpu,
            ram = self.ram/1024,
            disks = len(self.blockDevices),
            diskType = self.diskType,
            diskCapacities = ",".join(str(capacity) for capacity in self.diskCapacities),
            bandwidth = self.bandwidth,
            bandwidthType = self.bandwidthType
        )

    @name.setter
    def name(self, value):
        # we do not need store the name since we auto generate it from the extra properties
        pass

    @property
    def ram(self):
        return self.extra.get("maxMemory", 0)

    @ram.setter
    def ram(self, value):
        # we do not need store the ram since we auto generate it from the extra properties
        pass

class SoftLayerPythonAPINodeDriver(NodeDriver):
    """
    SoftLayer node driver using the SoftLayer Python API

    """
    type = "SoftLayerPythonAPI"
    name = "SoftLayerPythonAPINodeDriver"
    features = {"create_node": ["generates_password"]}

    def __init__(self, username, apiKey):
        super(SoftLayerPythonAPINodeDriver, self).__init__(username, apiKey)
        self.client = SoftLayer.create_client_from_env(username=username, api_key=apiKey)

    def _get_additional_sizes(self, options, existingSizes):
        """
        Get a combination of existing sizes with the specified options

        :param options: configuration options
        :type options: [SoftLayer_Container_Virtual_Guest_Configuration_Option dict]
        :param existingSizes: existing sizes
        :type existingSizes: [:class:`SoftLayerPythonAPINodeSize`]
        :returns: list of new node sizes
        :rtype: [:class:`SoftLayerPythonAPINodeSize`]
        """
        newSizes = []
        for option in options:
            template =  SoftLayer.utils.lookup(option, "template")
            template.pop("id", None)
            for size in existingSizes:
                newSize = copy.deepcopy(size)
                # handle special case for block devices
                if "blockDevices" in template and "blockDevices" in newSize.extra:
                    newSize.extra["blockDevices"].append(template["blockDevices"][0])
                else:
                    newSize.extra.update(template)
                newSizes.append(newSize)
        return newSizes

    def _get_additional_SAN_disk_sizes(self, existingSizes):
        """
        Get a existing sizes with an additional SAN disk of the same capacity
        as the currently last one

        :param existingSizes: existing sizes
        :type existingSizes: [:class:`SoftLayerPythonAPINodeSize`]
        :returns: list of new node sizes
        :rtype: [:class:`SoftLayerPythonAPINodeSize`]
        """
        newSizes = []
        for size in existingSizes:
            newSize = copy.deepcopy(size)
            newBlockDevice = copy.deepcopy(newSize.extra["blockDevices"][-1])
            newBlockDevice["device"] = str(int(newBlockDevice["device"]) + 1)
            newSize.extra["blockDevices"].append(newBlockDevice)
            newSizes.append(newSize)
        return newSizes

    def _get_disk_size_options(self, options, deviceNumber, localDisk):
        """
        Get a combination of existing sizes with the specified options

        :param options: disk configuration options
        :type options: [SoftLayer_Container_Virtual_Guest_Configuration_Option dict]
        :param deviceNumber: specific device number to filter by
        :type deviceNumber: int
        :param localDisk: whether the disks is local
        :type localDisk: bool
        :returns: list of filtered disk configuration options
        :rtype: [SoftLayer_Container_Virtual_Guest_Configuration_Option dict]
        """
        filteredOptions = []
        for option in options:
            if (int(option["template"]["blockDevices"][0]["device"]) == deviceNumber
                and option["template"]["localDiskFlag"] == localDisk):
                # remove empty id
                option["template"].pop("id", None)
                filteredOptions.append(option)
        return filteredOptions

    def _get_image(self, image, details=False):
        '''
        Convert softlayer image information into NodeImage instance. Apart from guid
        figure out the number of disks (and their sizes) contained in this image

        :param dict image: image information obtained from `~SoftLayer.ImageManager`
        :returns: :class:`~libcloud.compute.base.NodeImage`
        '''
        extra = {"guid": image["globalIdentifier"]}
        extra['disks'] = {'num':0, 'details':[]}

        disks = self.client['Virtual_Guest_Block_Device_Template_Group'].getChildren(id=image['id'])
        for child in disks:
            blockDevices = self.client['Virtual_Guest_Block_Device_Template_Group'].getBlockDevices(id=child['id'])
            extra['disks']['num'] = len(blockDevices)
            if details:
                unitsConversion = {'B':3, 'K':2, 'M':1, 'G':0}  # bdev['units'] is 'M', 'B', 'K' or 'G'
                for dn, (size, units) in enumerate([ (bdev['diskSpace'], bdev['units'])
                                                    for bdev in blockDevices if 'diskSpace' in bdev ]):
                    size /= (1024 ** unitsConversion[units])
                    extra['disks']['details'].append(round(size, 0))

        return NodeImage(image["id"], image["name"], self, extra=extra)

    def _virtual_to_node(self, instance):
        """
        Convert a SoftLayer instance dictionary into

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
                disks.append(blockDevice["diskImage"]["capacity"])

        extra = {"disks" : {
                            "num": len(disks),
                            "details": disks
                            },
                 "domain" : instance["domain"],
                 "hostname": instance["hostname"],
                 "maxCpu": instance["maxCpu"],
                 "maxMemory": instance["maxMemory"]
        }

        if "billingItem" in instance and "categoryCode" in instance["billingItem"]:
            extra["category"] = instance["billingItem"]["categoryCode"]
        else:
            extra["category"] = "guest_core"

        if "powerState" in instance and "keyName" in instance["powerState"]:
            state = NODE_STATE_MAP.get(instance["powerState"]["keyName"], NodeState.UNKNOWN)
        else:
            state = NodeState.UNKNOWN

        try:
            extra["password"] = instance["operatingSystem"]["passwords"][0]["password"]
        except:
            extra["password"] = "unknown"

        # TODO: size and image
        return Node(instance["id"],
                    instance["fullyQualifiedDomainName"],
                    state,
                    publicIps,
                    privateIps,
                    self,
                    extra=extra)

    def create_node(self, wait=0, **kwargs):
        """
        Creates a new virtual server instance

        :param int cpus: The number of virtual CPUs to include in the instance.
        :param int memory: The amount of RAM to order.
        :param bool hourly: Flag to indicate if this server should be billed
                            hourly (default) or monthly.
        :param string hostname: The hostname to use for the new server.
        :param string domain: The domain to use for the new server.
        :param bool local_disk: Flag to indicate if this should be a local disk
                                (default) or a SAN disk.
        :param string datacenter: The short name of the data center in which
                                  the VS should reside.
        :param string os_code: The operating system to use. Cannot be specified
                               if image_id is specified.
        :param int image_id: The ID of the image to load onto the server.
                             Cannot be specified if os_code is specified.
        :param bool dedicated: Flag to indicate if this should be housed on a
                               dedicated or shared host (default). This will
                               incur a fee on your account.
        :param int public_vlan: The ID of the public VLAN on which you want
                                this VS placed.
        :param int private_vlan: The ID of the public VLAN on which you want
                                 this VS placed.
        :param list disks: A list of disk capacities for this server.
        :param string post_uri: The URI of the post-install script to run
                                after reload
        :param bool private: If true, the VS will be provisioned only with
                             access to the private network. Defaults to false
        :param list ssh_keys: The SSH keys to add to the root user
        :param int nic_speed: The port speed to set
        :param string tag: tags to set on the VS as a comma separated list
        """
        vs_manager = SoftLayer.VSManager(self.client)
        instanceInfo = vs_manager.create_instance(**kwargs)
        if wait > 0:
            vs_manager.wait_for_ready(instanceInfo['id'],wait, delay=5, pending=True)

        # make sure we include masks for information we need
        virtualkwargs = {"mask" : "mask[{0}]".format(",".join(VIRTUAL_INFO_ITEMS))}
        instance = vs_manager.get_instance(instanceInfo['id'], **virtualkwargs)

        return self._virtual_to_node(instance)

    def destroy_node(self, node):
        """ Cancel an instance immediately, deleting all its data.

        :param node: The node to be destroyed
        :type node: :class:`libcloud.compute.base.Node`
        """
        vs_manager = SoftLayer.VSManager(self.client)
        vs_manager.cancel_instance(int(node.id))

    def ex_create_image(self, instanceId, imageName, imageNotes=None):
        '''
        Capture and create a FLEX image for intanceId

        :param int instanceId: id of the instance to be 'captured'
        :param str imageName: name to be given to created image
        :param str imageNodes: additional notes for the image
        :return: True if image creation succeeded, else False
        :rtype: bool
        '''
        vs = SoftLayer.VSManager(self.client)
        img = SoftLayer.ImageManager(self.client)
        try:
            vs.capture(int(instanceId), imageName, additional_disks=True, notes=imageNotes)
            images = img.list_private_images(name=imageName)
            imageId = images[-1]['id']
	    done=0
            while True:
                idetail = self.client['Virtual_Guest_Block_Device_Template_Group'].getObject(id=imageId)
                if not idetail.get('transactionId'):  # we are all set if there is no transaction running
                    for chld in self.client['Virtual_Guest_Block_Device_Template_Group'].getChildren(id=imageId):
                        if not chld.get('transactionId'):
                            done += 1
                        else:
                            # 3 consecutive polls should mark tell us that there is indeed no transaction in progress
                            # before we can say that the template is done
                            done = 0
	                time.sleep(5)
                    if done == 3:
                        break
                time.sleep(5)
            return True
        except SoftLayer.exceptions.SoftLayerAPIError, err:
            log.error("cannot capture image for {0} : {1}".format(instanceId,
                        traceback.format_exception_only(SoftLayer.exceptions.SoftLayerAPIError, err)))
        return None

    def ex_create_nodes(self, configs, wait=0):
        """
        Create several instances

        :param configs: list of configurations
        :type configs: [dict]
        """
        totalStart = datetime.datetime.utcnow()
        vs_manager = SoftLayer.VSManager(self.client)
        instanceInfos = vs_manager.create_instances(configs)

        nodes = []
        transactions = {}
        readyInstances = set()
        while wait > 0:

            # go through all the nodes and check their current transactions
            for instanceInfo in instanceInfos:

                if instanceInfo["fullyQualifiedDomainName"] not in readyInstances:

                    instance = vs_manager.get_instance(instanceInfo["id"])
                    activeTransactionId = SoftLayer.utils.lookup(instance,
                                                      "activeTransaction",
                                                      "id")
                    activeTransactionName = SoftLayer.utils.lookup(instance,
                                                      "activeTransaction",
                                                      "transactionStatus",
                                                      "friendlyName")

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
                wait -= 1
                time.sleep(1)

        if len(readyInstances) != len(instanceInfos):
            log.info("Creating %d nodes timed out", len(instanceInfos))
            return nodes

        for instanceInfo in instanceInfos:
            # make sure we include masks for information we need
            virtualkwargs = {"mask" : "mask[{0}]".format(",".join(VIRTUAL_INFO_ITEMS))}
            instance = vs_manager.get_instance(instanceInfo['id'], **virtualkwargs)
            nodes.append(self._virtual_to_node(instance))

        totalEnd = datetime.datetime.utcnow()
        log.info("Creating %d nodes took %s", len(instanceInfos), totalEnd-totalStart)

        return nodes

    def ex_get_hardware_nodes(self, hostname=None, domain=None):
        """
        Get a list of hardware nodes (server and bare metal), optionally filtered by hostname and domain

        :param str hostname: A hostname or pattern to filter list of nodes
        :param str domain: A domain or pattern to filter list of nodes
        :return: [:class:`~libcloud.compute.base.Node`]
        """
        nodes = []

        # make sure we include masks for information we need
        hw_items = [
            'id',
            'hostname',
            'domain',
            'hardwareStatusId',
            'globalIdentifier',
            'fullyQualifiedDomainName',
            'processorPhysicalCoreAmount',
            'memoryCapacity',
            'primaryBackendIpAddress',
            'primaryIpAddress',
            'datacenter',
            'billingItem.orderItem.order.userRecord[username]',
            'operatingSystem.passwords', # include passwords
            'bareMetalInstanceFlag', # include whether this is bare metal
            'activeComponents' # include hardware components such as disks
        ]
        server_items = [
            'activeTransaction[id, transactionStatus[friendlyName,name]]',
        ]

        hardwarekwargs = {"mask" : "[mask[%s],mask(SoftLayer_Hardware_Server)[%s]]"
                          % (','.join(hw_items), ','.join(server_items))}
        hardwareManager = SoftLayer.HardwareManager(self.client)

        for hardware in hardwareManager.list_hardware(hostname=hostname, domain=domain, **hardwarekwargs):

            publicIps = []
            if "primaryIpAddress" in hardware:
                publicIps.append(hardware["primaryIpAddress"])
            privateIps = []
            if "primaryBackendIpAddress" in hardware:
                privateIps.append(hardware["primaryBackendIpAddress"])

            disks = []
            for activeComponent in hardware.get("activeComponents", []):

                hardwareComponentType = activeComponent["hardwareComponentModel"]["hardwareGenericComponentModel"]["hardwareComponentType"]

                if hardwareComponentType["keyName"] == "HARD_DRIVE":
                    # note that the default unit is GB
                    disks.append(activeComponent["hardwareComponentModel"]["capacity"])

            # TODO: add memory information
            # TODO: add cpu information
            extra = {"disks" : {
                                "num": len(disks),
                                "details": disks
                                },
                     "domain" : hardware["domain"],
                     "hostname": hardware["hostname"]
                     }

            if "billingItem" in hardware and "categoryCode" in hardware["billingItem"]:
                extra["category"] = hardware["billingItem"]["categoryCode"]
            else:
                extra["category"] = "server"

            try:
                extra["password"] = hardware["operatingSystem"]["passwords"][0]["password"]
            except:
                extra["password"] = "unknown"

            state = NodeState.RUNNING

            # TODO: node size and image
            nodes.append(Node(hardware["id"],
                              hardware["fullyQualifiedDomainName"],
                              state,
                              publicIps,
                              privateIps,
                              self,
                              extra=extra))

        nodes = sorted(nodes, key=lambda node: node.name)
        return nodes

    def ex_get_image(self, imageid):
        '''
        Lookup and return NodeImage instance for given imageid

        :param int imageid: SoftLayer Image id
        :returns: :class:`~libcloud.compute.base.NodeImage`
        '''
        imageManager = SoftLayer.ImageManager(self.client)
        return self._get_image(imageManager.get_image(imageid))

    def ex_get_virtual_nodes(self, hostname=None, domain=None):
        """
        Get a list of virtual nodes, optionally filtered by hostname and domain

        :param str hostname: A hostname or pattern to filter list of nodes
        :param str domain: A domain or pattern to filter list of nodes
        :returns: [:class:`~libcloud.compute.base.Node`]
        """
        nodes = []
        vs = SoftLayer.VSManager(self.client)

        # make sure we include masks for information we need
        virtualkwargs = {"mask" : "mask[{0}]".format(",".join(VIRTUAL_INFO_ITEMS))}
        for instance in vs.list_instances(hostname=hostname, domain=domain, **virtualkwargs):
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

    def list_images(self, location=None, listPublic=False, details=False):
        """
        Get a list of images

        :param location: location
        :type location: :class:`~libcloud.compute.base.NodeLocation`
        :param bool listPublic: determines whether to include public images
        :param bool details: determines whether to include detailed information (e.g. disk sizes)
        :returns: [:class:`~libcloud.compute.base.NodeImage`]
        """
        # TODO: incorporate location
        images = []
        imageManager = SoftLayer.ImageManager(self.client)
        softlayerImages = imageManager.list_private_images()
        if listPublic:
            softlayerImages.extend(imageManager.list_public_images())
        softlayerImages = sorted(softlayerImages, key=lambda image: image["name"])
        for image in softlayerImages:
            images.append(self._get_image(image, details))
        return images

    def list_locations(self):
        """
        Get a list of data centers for a provider

        :returns: [:class:`SoftLayerPythonAPINodeLocation`]
        """
        locations = []
        datacenters = self.client["Location"].getDatacenters()
        for datacenter in datacenters:

            address = self.client["Location"].getLocationAddress(id=datacenter["id"])
            try:
                extra = {"city": address["city"],
                         "description": address["description"],
                         "longName": datacenter["longName"]
                         }
                # TODO: use longName or name?
                locations.append(SoftLayerPythonAPINodeLocation(datacenter["id"],
                                              datacenter["name"],
                                              address["country"],
                                              self,
                                              extra=extra))
            except:
                # FIXME: figure out why location is not available for theses
                import pprint
                log.fatal(pprint.pformat(datacenter))
                log.fatal(pprint.pformat(address))
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

    def ex_list_nodes(self, hostname=None, domain=None):
        '''
        Get a list of virtual nodes, optionally filtered by hostname and domain

        :param str hostname: A hostname or pattern to filter list of nodes
        :param str domain: A domain or pattern to filter list of nodes
        :returns: [:class:`~libcloud.compute.base.Node`]
        '''
        nodes = self.ex_get_hardware_nodes(hostname=hostname, domain=domain)
        nodes.extend(self.ex_get_virtual_nodes(hostname=hostname, domain=domain))
        return sorted(nodes, key=lambda node: node.name)

    def list_sizes(self, location=None):
        """
        List sizes on a provider

        :param location: The location at which to list sizes
        :type location: :class:`.NodeLocation`

        :return: list of node size objects
        :rtype: ``list`` of :class:`.NodeSize`
        """
        sizes = []
        vs = SoftLayer.VSManager(self.client)
        virtualMachineOptions = vs.get_create_options()

        sizes.append(SoftLayerPythonAPINodeSize(1, "", 0, 0, 0, 0.0, None, {}))

        # add different component sizes
        sizes = self._get_additional_sizes(virtualMachineOptions["processors"], sizes)
        sizes = self._get_additional_sizes(virtualMachineOptions["memory"], sizes)
        sizes = self._get_additional_sizes(virtualMachineOptions["networkComponents"], sizes)
        # TODO: determine need for incorporating operating system

        # get disk options for the first two disks based on their type
        localDisk0Options = self._get_disk_size_options(virtualMachineOptions["blockDevices"], 0, True)
        localDisk2Options = self._get_disk_size_options(virtualMachineOptions["blockDevices"], 2, True)
        sanDisk0Options = self._get_disk_size_options(virtualMachineOptions["blockDevices"], 0, False)
        sanDisk2Options = self._get_disk_size_options(virtualMachineOptions["blockDevices"], 2, False)

        # get different size based on the number and type of disks
        oneLocalDiskSizes = self._get_additional_sizes(localDisk0Options, sizes)
        twoLocalDiskSizes = self._get_additional_sizes(localDisk2Options, oneLocalDiskSizes)
        oneSANDiskSizes = self._get_additional_sizes(sanDisk0Options, sizes)
        twoSANDiskSizes = self._get_additional_sizes(sanDisk2Options, oneSANDiskSizes)
        # note that for more than 2 SAN disks we choose uniform disk size since the list otherwise becomes unmanageable
        threeSANDiskSizes = self._get_additional_SAN_disk_sizes(twoSANDiskSizes)
        fourSANDiskSizes = self._get_additional_SAN_disk_sizes(threeSANDiskSizes)
        fiveSANDiskSizes = self._get_additional_SAN_disk_sizes(fourSANDiskSizes)

        sizes = oneLocalDiskSizes + twoLocalDiskSizes + oneSANDiskSizes + twoSANDiskSizes + threeSANDiskSizes + fourSANDiskSizes + fiveSANDiskSizes

        # adjust id and driver properties
        sizeId = 1
        for size in sizes:
            size.id = sizeId
            size.driver = self
            sizeId += 1
        return sizes

def slcli():
    """
    Pass-through to SoftLayer commandline client
    """
    import SoftLayer.CLI.core
    SoftLayer.CLI.core.main()

set_driver(SoftLayerPythonAPINodeDriver.type, SoftLayerPythonAPINodeDriver.__module__, SoftLayerPythonAPINodeDriver.name)

if __name__ == '__main__':
    slcli()
