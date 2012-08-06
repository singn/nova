# vim: tabstop=4 shiftwidth=4 softtabstop=4

# Copyright (c) 2012 NetApp, Inc.
# Copyright (c) 2012 OpenStack LLC.
# All Rights Reserved.
#
#    Licensed under the Apache License, Version 2.0 (the "License"); you may
#    not use this file except in compliance with the License. You may obtain
#    a copy of the License at
#
#         http://www.apache.org/licenses/LICENSE-2.0
#
#    Unless required by applicable law or agreed to in writing, software
#    distributed under the License is distributed on an "AS IS" BASIS, WITHOUT
#    WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied. See the
#    License for the specific language governing permissions and limitations
#    under the License.
"""
Volume driver for NetApp storage systems.

This driver requires NetApp OnCommand 5.0 and one or more Data
ONTAP 7-mode storage systems with installed iSCSI licenses.

"""

import string
import time

import suds
from suds import client
from suds.sax import text

from nova import exception
from nova import flags
from nova.openstack.common import cfg
from nova.openstack.common import log as logging
from nova.volume import driver
from nova.volume import volume_types

LOG = logging.getLogger(__name__)

netapp_opts = [
    cfg.StrOpt('netapp_wsdl_url',
               default=None,
               help='URL of the WSDL file for the DFM server'),
    cfg.StrOpt('netapp_login',
               default=None,
               help='User name for the DFM server'),
    cfg.StrOpt('netapp_password',
               default=None,
               help='Password for the DFM server'),
    cfg.StrOpt('netapp_server_hostname',
               default=None,
               help='Hostname for the DFM server'),
    cfg.IntOpt('netapp_server_port',
               default=8088,
               help='Port number for the DFM server'),
    cfg.StrOpt('netapp_storage_service',
               default=None,
               help=('Storage service to use for provisioning '
                    '(when volume_type=None)')),
    cfg.StrOpt('netapp_storage_service_prefix',
               default=None,
               help=('Prefix of storage service name to use for '
                    'provisioning (volume_type name will be appended)')),
    cfg.StrOpt('netapp_vfiler',
               default=None,
               help='Vfiler to use for provisioning'),
    ]

FLAGS = flags.FLAGS
FLAGS.register_opts(netapp_opts)


class DfmDataset(object):
    def __init__(self, id, name, project, type):
        self.id = id
        self.name = name
        self.project = project
        self.type = type


class DfmLun(object):
    def __init__(self, dataset, lunpath, id):
        self.dataset = dataset
        self.lunpath = lunpath
        self.id = id


class NetAppISCSIDriver(driver.ISCSIDriver):
    """NetApp iSCSI volume driver."""

    IGROUP_PREFIX = 'openstack-'
    DATASET_PREFIX = 'OpenStack_'
    DATASET_METADATA_PROJECT_KEY = 'OpenStackProject'
    DATASET_METADATA_VOL_TYPE_KEY = 'OpenStackVolType'

    def __init__(self, *args, **kwargs):
        super(NetAppISCSIDriver, self).__init__(*args, **kwargs)
        self.discovered_luns = []
        self.discovered_datasets = []
        self.lun_table = {}

    def _check_fail(self, request, response):
        """Utility routine to handle checking ZAPI failures."""
        if 'failed' == response.Status:
            name = request.Name
            reason = response.Reason
            msg = _('API %(name)s failed: %(reason)s')
            raise exception.NovaException(msg % locals())

    def _create_client(self, **kwargs):
        """Instantiate a web services client.

        This method creates a "suds" client to make web services calls to the
        DFM server. Note that the WSDL file is quite large and may take
        a few seconds to parse.
        """
        wsdl_url = kwargs['wsdl_url']
        LOG.debug(_('Using WSDL: %s') % wsdl_url)
        if kwargs['cache']:
            self.client = client.Client(wsdl_url, username=kwargs['login'],
                                        password=kwargs['password'])
        else:
            self.client = client.Client(wsdl_url, username=kwargs['login'],
                                        password=kwargs['password'],
                                        cache=None)
        soap_url = 'http://%s:%s/apis/soap/v1' % (kwargs['hostname'],
                                                  kwargs['port'])
        LOG.debug(_('Using DFM server: %s') % soap_url)
        self.client.set_options(location=soap_url)

    def _set_storage_service(self, storage_service):
        """Set the storage service to use for provisioning."""
        LOG.debug(_('Using storage service: %s') % storage_service)
        self.storage_service = storage_service

    def _set_storage_service_prefix(self, storage_service_prefix):
        """Set the storage service prefix to use for provisioning."""
        LOG.debug(_('Using storage service prefix: %s') %
                  storage_service_prefix)
        self.storage_service_prefix = storage_service_prefix

    def _set_vfiler(self, vfiler):
        """Set the vfiler to use for provisioning."""
        LOG.debug(_('Using vfiler: %s') % vfiler)
        self.vfiler = vfiler

    def _check_flags(self):
        """Ensure that the flags we care about are set."""
        required_flags = ['netapp_wsdl_url', 'netapp_login', 'netapp_password',
                'netapp_server_hostname', 'netapp_server_port']
        for flag in required_flags:
            if not getattr(FLAGS, flag, None):
                raise exception.NovaException(_('%s is not set') % flag)
        if not (FLAGS.netapp_storage_service or
                FLAGS.netapp_storage_service_prefix):
            raise exception.NovaException(_('Either netapp_storage_service or '
                'netapp_storage_service_prefix must be set'))

    def do_setup(self, context):
        """Setup the NetApp Volume driver.

        Called one time by the manager after the driver is loaded.
        Validate the flags we care about and setup the suds (web services)
        client.
        """
        self._check_flags()
        self._create_client(wsdl_url=FLAGS.netapp_wsdl_url,
            login=FLAGS.netapp_login, password=FLAGS.netapp_password,
            hostname=FLAGS.netapp_server_hostname,
            port=FLAGS.netapp_server_port, cache=True)
        self._set_storage_service(FLAGS.netapp_storage_service)
        self._set_storage_service_prefix(FLAGS.netapp_storage_service_prefix)
        self._set_vfiler(FLAGS.netapp_vfiler)

    def check_for_setup_error(self):
        """Check that the driver is working and can communicate.

        Invoke a web services API to make sure we can talk to the server.
        Also perform the discovery of datasets and LUNs from DFM.
        """
        self.client.service.DfmAbout()
        LOG.debug(_("Connected to DFM server"))
        self._discover_luns()

    def _get_datasets(self):
        """Get the list of datasets from DFM."""
        server = self.client.service
        res = server.DatasetListInfoIterStart(IncludeMetadata=True)
        tag = res.Tag
        datasets = []
        try:
            while True:
                res = server.DatasetListInfoIterNext(Tag=tag, Maximum=100)
                if not res.Datasets:
                    break
                datasets.extend(res.Datasets.DatasetInfo)
        finally:
            server.DatasetListInfoIterEnd(Tag=tag)
        return datasets

    def _discover_dataset_luns(self, dataset, volume):
        """Discover all of the LUNs in a dataset."""
        server = self.client.service
        res = server.DatasetMemberListInfoIterStart(
                DatasetNameOrId=dataset.id,
                IncludeExportsInfo=True,
                IncludeIndirect=True,
                MemberType='lun_path')
        tag = res.Tag
        suffix = None
        if volume:
            suffix = '/' + volume
        try:
            while True:
                res = server.DatasetMemberListInfoIterNext(Tag=tag,
                                                           Maximum=100)
                if (not hasattr(res, 'DatasetMembers') or
                            not res.DatasetMembers):
                    break
                for member in res.DatasetMembers.DatasetMemberInfo:
                    if suffix and not member.MemberName.endswith(suffix):
                        continue
                    # MemberName is the full LUN path in this format:
                    # host:/volume/qtree/lun
                    lun = DfmLun(dataset, member.MemberName, member.MemberId)
                    self.discovered_luns.append(lun)
        finally:
            server.DatasetMemberListInfoIterEnd(Tag=tag)

    def _discover_luns(self):
        """Discover the LUNs from DFM.

        Discover all of the OpenStack-created datasets and LUNs in the DFM
        database.
        """
        datasets = self._get_datasets()
        self.discovered_datasets = []
        self.discovered_luns = []
        for dataset in datasets:
            if not dataset.DatasetName.startswith(self.DATASET_PREFIX):
                continue
            if (not hasattr(dataset, 'DatasetMetadata') or
                    not dataset.DatasetMetadata):
                continue
            project = None
            type = None
            for field in dataset.DatasetMetadata.DfmMetadataField:
                if field.FieldName == self.DATASET_METADATA_PROJECT_KEY:
                    project = field.FieldValue
                elif field.FieldName == self.DATASET_METADATA_VOL_TYPE_KEY:
                    type = field.FieldValue
            if not project:
                continue
            ds = DfmDataset(dataset.DatasetId, dataset.DatasetName,
                            project, type)
            self.discovered_datasets.append(ds)
            self._discover_dataset_luns(ds, None)
        dataset_count = len(self.discovered_datasets)
        lun_count = len(self.discovered_luns)
        msg = _("Discovered %(dataset_count)s datasets and %(lun_count)s LUNs")
        LOG.debug(msg % locals())
        self.lun_table = {}

    def _get_job_progress(self, job_id):
        """Get progress of one running DFM job.

        Obtain the latest progress report for the job and return the
        list of progress events.
        """
        server = self.client.service
        res = server.DpJobProgressEventListIterStart(JobId=job_id)
        tag = res.Tag
        event_list = []
        try:
            while True:
                res = server.DpJobProgressEventListIterNext(Tag=tag,
                                                            Maximum=100)
                if not hasattr(res, 'ProgressEvents'):
                    break
                event_list += res.ProgressEvents.DpJobProgressEventInfo
        finally:
            server.DpJobProgressEventListIterEnd(Tag=tag)
        return event_list

    def _wait_for_job(self, job_id):
        """Wait until a job terminates.

        Poll the job until it completes or an error is detected. Return the
        final list of progress events if it completes successfully.
        """
        while True:
            events = self._get_job_progress(job_id)
            for event in events:
                if event.EventStatus == 'error':
                    raise exception.NovaException(_('Job failed: %s') %
                        (event.ErrorMessage))
                if event.EventType == 'job-end':
                    return events
            time.sleep(5)

    def _dataset_name(self, project, ss_type):
        """Return the dataset name for a given project and volume type."""
        _project = project.replace(' ', '_').replace('-', '_')
        dataset_name = self.DATASET_PREFIX + _project
        if not ss_type:
            return dataset_name
        _type = ss_type.replace(' ', '_').replace('-', '_')
        return dataset_name + '_' + _type

    def _get_dataset(self, dataset_name):
        """Lookup a dataset by name in the list of discovered datasets."""
        for dataset in self.discovered_datasets:
            if dataset.name == dataset_name:
                return dataset
        return None

    def _create_dataset(self, dataset_name, project, ss_type):
        """Create a new dataset using the storage service.

        The export settings are set to create iSCSI LUNs aligned for Linux.
        Returns the ID of the new dataset.
        """
        if ss_type and not self.storage_service_prefix:
            msg = _('Attempt to use volume_type without specifying '
                'netapp_storage_service_prefix flag.')
            raise exception.NovaException(msg)
        if not (ss_type or self.storage_service):
            msg = _('You must set the netapp_storage_service flag in order to '
                'create volumes with no volume_type.')
            raise exception.NovaException(msg)
        storage_service = self.storage_service
        if ss_type:
            storage_service = self.storage_service_prefix + ss_type

        factory = self.client.factory

        lunmap = factory.create('DatasetLunMappingInfo')
        lunmap.IgroupOsType = 'linux'
        export = factory.create('DatasetExportInfo')
        export.DatasetExportProtocol = 'iscsi'
        export.DatasetLunMappingInfo = lunmap
        detail = factory.create('StorageSetInfo')
        detail.DpNodeName = 'Primary data'
        detail.DatasetExportInfo = export
        if hasattr(self, 'vfiler') and self.vfiler:
            detail.ServerNameOrId = self.vfiler
        details = factory.create('ArrayOfStorageSetInfo')
        details.StorageSetInfo = [detail]
        field1 = factory.create('DfmMetadataField')
        field1.FieldName = self.DATASET_METADATA_PROJECT_KEY
        field1.FieldValue = project
        field2 = factory.create('DfmMetadataField')
        field2.FieldName = self.DATASET_METADATA_VOL_TYPE_KEY
        field2.FieldValue = ss_type
        metadata = factory.create('ArrayOfDfmMetadataField')
        metadata.DfmMetadataField = [field1, field2]

        res = self.client.service.StorageServiceDatasetProvision(
                StorageServiceNameOrId=storage_service,
                DatasetName=dataset_name,
                AssumeConfirmation=True,
                StorageSetDetails=details,
                DatasetMetadata=metadata)

        ds = DfmDataset(res.DatasetId, dataset_name, project, ss_type)
        self.discovered_datasets.append(ds)
        return ds

    def _provision(self, name, description, project, ss_type, size):
        """Provision a LUN through provisioning manager.

        The LUN will be created inside a dataset associated with the project.
        If the dataset doesn't already exist, we create it using the storage
        service specified in the nova conf.
        """
        dataset_name = self._dataset_name(project, ss_type)
        dataset = self._get_dataset(dataset_name)
        if not dataset:
            dataset = self._create_dataset(dataset_name, project, ss_type)

        info = self.client.factory.create('ProvisionMemberRequestInfo')
        info.Name = name
        if description:
            info.Description = description
        info.Size = size
        info.MaximumSnapshotSpace = 2 * long(size)

        server = self.client.service
        lock_id = server.DatasetEditBegin(DatasetNameOrId=dataset.id)
        try:
            server.DatasetProvisionMember(EditLockId=lock_id,
                                          ProvisionMemberRequestInfo=info)
            res = server.DatasetEditCommit(EditLockId=lock_id,
                                           AssumeConfirmation=True)
        except (suds.WebFault, Exception):
            server.DatasetEditRollback(EditLockId=lock_id)
            msg = _('Failed to provision dataset member')
            raise exception.NovaException(msg)

        lun_id = None
        lunpath = None

        for info in res.JobIds.JobInfo:
            events = self._wait_for_job(info.JobId)
            for event in events:
                if event.EventType != 'lun-create':
                    continue
                lunpath = event.ProgressLunInfo.LunName
                lun_id = event.ProgressLunInfo.LunPathId

        if not lun_id:
            msg = _('No LUN was created by the provision job')
            raise exception.NovaException(msg)

        lun = DfmLun(dataset, lunpath, lun_id)
        self.discovered_luns.append(lun)
        self.lun_table[name] = lun

    def _get_ss_type(self, volume):
        """Get the storage service type for a volume."""
        id = volume['volume_type_id']
        if not id:
            return None
        volume_type = volume_types.get_volume_type(None, id)
        if not volume_type:
            return None
        return volume_type['name']

    def _remove_destroy(self, name, project):
        """Remove the LUN from the dataset, also destroying it.

        Remove the LUN from the dataset and destroy the actual LUN on the
        storage system.
        """
        lun = self._lookup_lun_for_volume(name, project)
        member = self.client.factory.create('DatasetMemberParameter')
        member.ObjectNameOrId = lun.id
        members = self.client.factory.create('ArrayOfDatasetMemberParameter')
        members.DatasetMemberParameter = [member]

        server = self.client.service
        lock_id = server.DatasetEditBegin(DatasetNameOrId=lun.dataset.id)
        try:
            server.DatasetRemoveMember(EditLockId=lock_id, Destroy=True,
                                       DatasetMemberParameters=members)
            server.DatasetEditCommit(EditLockId=lock_id,
                                     AssumeConfirmation=True)
        except (suds.WebFault, Exception):
            server.DatasetEditRollback(EditLockId=lock_id)
            msg = _('Failed to remove and delete dataset member')
            raise exception.NovaException(msg)

    def create_volume(self, volume):
        """Driver entry point for creating a new volume."""
        default_size = '104857600'  # 100 MB
        gigabytes = 1073741824L  # 2^30
        name = volume['name']
        project = volume['project_id']
        display_name = volume['display_name']
        display_description = volume['display_description']
        description = None
        if display_name:
            if display_description:
                description = display_name + "\n" + display_description
            else:
                description = display_name
        elif display_description:
            description = display_description
        if int(volume['size']) == 0:
            size = default_size
        else:
            size = str(int(volume['size']) * gigabytes)
        ss_type = self._get_ss_type(volume)
        self._provision(name, description, project, ss_type, size)

    def _lookup_lun_for_volume(self, name, project):
        """Lookup the LUN that corresponds to the give volume.

        Initial lookups involve a table scan of all of the discovered LUNs,
        but later lookups are done instantly from the hashtable.
        """
        if name in self.lun_table:
            return self.lun_table[name]
        lunpath_suffix = '/' + name
        for lun in self.discovered_luns:
            if lun.dataset.project != project:
                continue
            if lun.lunpath.endswith(lunpath_suffix):
                self.lun_table[name] = lun
                return lun
        msg = _("No entry in LUN table for volume %s")
        raise exception.NovaException(msg % (name))

    def delete_volume(self, volume):
        """Driver entry point for destroying existing volumes."""
        name = volume['name']
        project = volume['project_id']
        self._remove_destroy(name, project)

    def _get_lun_details(self, lun_id):
        """Given the ID of a LUN, get the details about that LUN."""
        server = self.client.service
        res = server.LunListInfoIterStart(ObjectNameOrId=lun_id)
        tag = res.Tag
        try:
            res = server.LunListInfoIterNext(Tag=tag, Maximum=1)
            if hasattr(res, 'Luns') and res.Luns.LunInfo:
                return res.Luns.LunInfo[0]
        finally:
            server.LunListInfoIterEnd(Tag=tag)
        msg = _('Failed to get LUN details for LUN ID %s')
        raise exception.NovaException(msg % lun_id)

    def _get_host_details(self, host_id):
        """Given the ID of a host, get the details about it.

        A "host" is a storage system here.
        """
        server = self.client.service
        res = server.HostListInfoIterStart(ObjectNameOrId=host_id)
        tag = res.Tag
        try:
            res = server.HostListInfoIterNext(Tag=tag, Maximum=1)
            if hasattr(res, 'Hosts') and res.Hosts.HostInfo:
                return res.Hosts.HostInfo[0]
        finally:
            server.HostListInfoIterEnd(Tag=tag)
        msg = _('Failed to get host details for host ID %s')
        raise exception.NovaException(msg % host_id)

    def _get_iqn_for_host(self, host_id):
        """Get the iSCSI Target Name for a storage system."""
        request = self.client.factory.create('Request')
        request.Name = 'iscsi-node-get-name'
        response = self.client.service.ApiProxy(Target=host_id,
                                                Request=request)
        self._check_fail(request, response)
        return response.Results['node-name'][0]

    def _api_elem_is_empty(self, elem):
        """Return true if the API element should be considered empty.

        Helper routine to figure out if a list returned from a proxy API
        is empty. This is necessary because the API proxy produces nasty
        looking XML.
        """
        if not type(elem) is list:
            return True
        if 0 == len(elem):
            return True
        child = elem[0]
        if isinstance(child, text.Text):
            return True
        if type(child) is str:
            return True
        return False

    def _get_target_portal_for_host(self, host_id, host_address):
        """Get iSCSI target portal for a storage system.

        Get the iSCSI Target Portal details for a particular IP address
        on a storage system.
        """
        request = self.client.factory.create('Request')
        request.Name = 'iscsi-portal-list-info'
        response = self.client.service.ApiProxy(Target=host_id,
                                                Request=request)
        self._check_fail(request, response)
        portal = {}
        portals = response.Results['iscsi-portal-list-entries']
        if self._api_elem_is_empty(portals):
            return portal
        portal_infos = portals[0]['iscsi-portal-list-entry-info']
        for portal_info in portal_infos:
            portal['address'] = portal_info['ip-address'][0]
            portal['port'] = portal_info['ip-port'][0]
            portal['portal'] = portal_info['tpgroup-tag'][0]
            if host_address == portal['address']:
                break
        return portal

    def _get_export(self, volume):
        """Get the iSCSI export details for a volume.

        Looks up the LUN in DFM based on the volume and project name, then get
        the LUN's ID. We store that value in the database instead of the iSCSI
        details because we will not have the true iSCSI details until masking
        time (when initialize_connection() is called).
        """
        name = volume['name']
        project = volume['project_id']
        lun = self._lookup_lun_for_volume(name, project)
        return {'provider_location': lun.id}

    def ensure_export(self, context, volume):
        """Driver entry point to get the export info for an existing volume."""
        return self._get_export(volume)

    def create_export(self, context, volume):
        """Driver entry point to get the export info for a new volume."""
        return self._get_export(volume)

    def remove_export(self, context, volume):
        """Driver exntry point to remove an export for a volume.

        Since exporting is idempotent in this driver, we have nothing
        to do for unexporting.
        """
        pass

    def _find_igroup_for_initiator(self, host_id, initiator_name):
        """Get the igroup for an initiator.

        Look for an existing igroup (initiator group) on the storage system
        containing a given iSCSI initiator and return the name of the igroup.
        """
        request = self.client.factory.create('Request')
        request.Name = 'igroup-list-info'
        response = self.client.service.ApiProxy(Target=host_id,
                                                Request=request)
        self._check_fail(request, response)
        igroups = response.Results['initiator-groups']
        if self._api_elem_is_empty(igroups):
            return None
        igroup_infos = igroups[0]['initiator-group-info']
        for igroup_info in igroup_infos:
            if ('iscsi' != igroup_info['initiator-group-type'][0] or
                'linux' != igroup_info['initiator-group-os-type'][0]):
                continue
            igroup_name = igroup_info['initiator-group-name'][0]
            if not igroup_name.startswith(self.IGROUP_PREFIX):
                continue
            initiators = igroup_info['initiators'][0]['initiator-info']
            for initiator in initiators:
                if initiator_name == initiator['initiator-name'][0]:
                    return igroup_name
        return None

    def _create_igroup(self, host_id, initiator_name):
        """Create a new igroup.

        Create a new igroup (initiator group) on the storage system to hold
        the given iSCSI initiator. The group will only have 1 member and will
        be named "openstack-${initiator_name}".
        """
        igroup_name = self.IGROUP_PREFIX + initiator_name
        request = self.client.factory.create('Request')
        request.Name = 'igroup-create'
        igroup_create_xml = (
            '<initiator-group-name>%s</initiator-group-name>'
            '<initiator-group-type>iscsi</initiator-group-type>'
            '<os-type>linux</os-type><ostype>linux</ostype>')
        request.Args = text.Raw(igroup_create_xml % igroup_name)
        response = self.client.service.ApiProxy(Target=host_id,
                                                Request=request)
        self._check_fail(request, response)
        request = self.client.factory.create('Request')
        request.Name = 'igroup-add'
        igroup_add_xml = (
            '<initiator-group-name>%s</initiator-group-name>'
            '<initiator>%s</initiator>')
        request.Args = text.Raw(igroup_add_xml % (igroup_name, initiator_name))
        response = self.client.service.ApiProxy(Target=host_id,
                                                Request=request)
        self._check_fail(request, response)
        return igroup_name

    def _get_lun_mappping(self, host_id, lunpath, igroup_name):
        """Get the mapping between a LUN and an igroup.

        Check if a given LUN is already mapped to the given igroup (initiator
        group). If the LUN is mapped, also return the LUN number for the
        mapping.
        """
        request = self.client.factory.create('Request')
        request.Name = 'lun-map-list-info'
        request.Args = text.Raw('<path>%s</path>' % (lunpath))
        response = self.client.service.ApiProxy(Target=host_id,
                                                 Request=request)
        self._check_fail(request, response)
        igroups = response.Results['initiator-groups']
        if self._api_elem_is_empty(igroups):
            return {'mapped': False}
        igroup_infos = igroups[0]['initiator-group-info']
        for igroup_info in igroup_infos:
            if igroup_name == igroup_info['initiator-group-name'][0]:
                return {'mapped': True, 'lun_num': igroup_info['lun-id'][0]}
        return {'mapped': False}

    def _map_initiator(self, host_id, lunpath, igroup_name):
        """Map a LUN to an igroup.

        Map the given LUN to the given igroup (initiator group). Return the LUN
        number that the LUN was mapped to (the filer will choose the lowest
        available number).
        """
        request = self.client.factory.create('Request')
        request.Name = 'lun-map'
        lun_map_xml = ('<initiator-group>%s</initiator-group>'
                       '<path>%s</path>')
        request.Args = text.Raw(lun_map_xml % (igroup_name, lunpath))
        response = self.client.service.ApiProxy(Target=host_id,
                                                Request=request)
        self._check_fail(request, response)
        return response.Results['lun-id-assigned'][0]

    def _unmap_initiator(self, host_id, lunpath, igroup_name):
        """Unmap the given LUN from the given igroup (initiator group)."""
        request = self.client.factory.create('Request')
        request.Name = 'lun-unmap'
        lun_unmap_xml = ('<initiator-group>%s</initiator-group>'
                         '<path>%s</path>')
        request.Args = text.Raw(lun_unmap_xml % (igroup_name, lunpath))
        response = self.client.service.ApiProxy(Target=host_id,
                                                Request=request)
        self._check_fail(request, response)

    def _ensure_initiator_mapped(self, host_id, lunpath, initiator_name):
        """Ensure that a LUN is mapped to a particular initiator.

        Check if a LUN is mapped to a given initiator already and create
        the mapping if it is not. A new igroup will be created if needed.
        Returns the LUN number for the mapping between the LUN and initiator
        in both cases.
        """
        lunpath = '/vol/' + lunpath
        igroup_name = self._find_igroup_for_initiator(host_id, initiator_name)
        if not igroup_name:
            igroup_name = self._create_igroup(host_id, initiator_name)

        mapping = self._get_lun_mappping(host_id, lunpath, igroup_name)
        if mapping['mapped']:
            return mapping['lun_num']
        return self._map_initiator(host_id, lunpath, igroup_name)

    def _ensure_initiator_unmapped(self, host_id, lunpath, initiator_name):
        """Ensure that a LUN is not mapped to a particular initiator.

        Check if a LUN is mapped to a given initiator and remove the
        mapping if it is. This does not destroy the igroup.
        """
        lunpath = '/vol/' + lunpath
        igroup_name = self._find_igroup_for_initiator(host_id, initiator_name)
        if not igroup_name:
            return

        mapping = self._get_lun_mappping(host_id, lunpath, igroup_name)
        if mapping['mapped']:
            self._unmap_initiator(host_id, lunpath, igroup_name)

    def initialize_connection(self, volume, connector):
        """Driver entry point to attach a volume to an instance.

        Do the LUN masking on the storage system so the initiator can access
        the LUN on the target. Also return the iSCSI properties so the
        initiator can find the LUN. This implementation does not call
        _get_iscsi_properties() to get the properties because cannot store the
        LUN number in the database. We only find out what the LUN number will
        be during this method call so we construct the properties dictionary
        ourselves.
        """
        initiator_name = connector['initiator']
        lun_id = volume['provider_location']
        if not lun_id:
            msg = _("No LUN ID for volume %s")
            raise exception.NovaException(msg % volume['name'])
        lun = self._get_lun_details(lun_id)
        lun_num = self._ensure_initiator_mapped(lun.HostId, lun.LunPath,
                                                initiator_name)
        host = self._get_host_details(lun.HostId)
        portal = self._get_target_portal_for_host(host.HostId,
                                                  host.HostAddress)
        if not portal:
            msg = _('Failed to get target portal for filer: %s')
            raise exception.NovaException(msg % host.HostName)

        iqn = self._get_iqn_for_host(host.HostId)
        if not iqn:
            msg = _('Failed to get target IQN for filer: %s')
            raise exception.NovaException(msg % host.HostName)

        properties = {}
        properties['target_discovered'] = False
        (address, port) = (portal['address'], portal['port'])
        properties['target_portal'] = '%s:%s' % (address, port)
        properties['target_iqn'] = iqn
        properties['target_lun'] = lun_num
        properties['volume_id'] = volume['id']

        auth = volume['provider_auth']
        if auth:
            (auth_method, auth_username, auth_secret) = auth.split()

            properties['auth_method'] = auth_method
            properties['auth_username'] = auth_username
            properties['auth_password'] = auth_secret

        return {
            'driver_volume_type': 'iscsi',
            'data': properties,
        }

    def terminate_connection(self, volume, connector):
        """Driver entry point to unattach a volume from an instance.

        Unmask the LUN on the storage system so the given intiator can no
        longer access it.
        """
        initiator_name = connector['initiator']
        lun_id = volume['provider_location']
        if not lun_id:
            msg = _('No LUN ID for volume %s')
            raise exception.NovaException(msg % (volume['name']))
        lun = self._get_lun_details(lun_id)
        self._ensure_initiator_unmapped(lun.HostId, lun.LunPath,
                                        initiator_name)

    def _is_clone_done(self, host_id, clone_op_id, volume_uuid):
        """Check the status of a clone operation.

        Return True if done, False otherwise.
        """
        request = self.client.factory.create('Request')
        request.Name = 'clone-list-status'
        clone_list_status_xml = (
            '<clone-id><clone-id-info>'
            '<clone-op-id>%s</clone-op-id>'
            '<volume-uuid>%s</volume-uuid>'
            '</clone-id-info></clone-id>')
        request.Args = text.Raw(clone_list_status_xml % (clone_op_id,
                                                          volume_uuid))
        response = self.client.service.ApiProxy(Target=host_id,
                                                Request=request)
        self._check_fail(request, response)
        status = response.Results['status']
        if self._api_elem_is_empty(status):
            return False
        ops_info = status[0]['ops-info'][0]
        state = ops_info['clone-state'][0]
        return 'completed' == state

    def _clone_lun(self, host_id, src_path, dest_path, snap):
        """Create a clone of a NetApp LUN.

        The clone initially consumes no space and is not space reserved.
        """
        request = self.client.factory.create('Request')
        request.Name = 'clone-start'
        clone_start_xml = (
            '<source-path>%s</source-path><no-snap>%s</no-snap>'
            '<destination-path>%s</destination-path>')
        if snap:
            no_snap = 'false'
        else:
            no_snap = 'true'
        request.Args = text.Raw(clone_start_xml % (src_path, no_snap,
                                                    dest_path))
        response = self.client.service.ApiProxy(Target=host_id,
                                                Request=request)
        self._check_fail(request, response)
        clone_id = response.Results['clone-id'][0]
        clone_id_info = clone_id['clone-id-info'][0]
        clone_op_id = clone_id_info['clone-op-id'][0]
        volume_uuid = clone_id_info['volume-uuid'][0]
        while not self._is_clone_done(host_id, clone_op_id, volume_uuid):
            time.sleep(5)

    def _refresh_dfm_luns(self, host_id):
        """Refresh the LUN list for one filer in DFM."""
        server = self.client.service
        server.DfmObjectRefresh(ObjectNameOrId=host_id, ChildType='lun_path')
        while True:
            time.sleep(15)
            res = server.DfmMonitorTimestampList(HostNameOrId=host_id)
            for timestamp in res.DfmMonitoringTimestamp:
                if 'lun' != timestamp.MonitorName:
                    continue
                if timestamp.LastMonitoringTimestamp:
                    return

    def _destroy_lun(self, host_id, lun_path):
        """Destroy a LUN on the filer."""
        request = self.client.factory.create('Request')
        request.Name = 'lun-offline'
        path_xml = '<path>%s</path>'
        request.Args = text.Raw(path_xml % lun_path)
        response = self.client.service.ApiProxy(Target=host_id,
                                                Request=request)
        self._check_fail(request, response)
        request = self.client.factory.create('Request')
        request.Name = 'lun-destroy'
        request.Args = text.Raw(path_xml % lun_path)
        response = self.client.service.ApiProxy(Target=host_id,
                                                Request=request)
        self._check_fail(request, response)

    def _resize_volume(self, host_id, vol_name, new_size):
        """Resize the volume by the amount requested."""
        request = self.client.factory.create('Request')
        request.Name = 'volume-size'
        volume_size_xml = (
            '<volume>%s</volume><new-size>%s</new-size>')
        request.Args = text.Raw(volume_size_xml % (vol_name, new_size))
        response = self.client.service.ApiProxy(Target=host_id,
                                                Request=request)
        self._check_fail(request, response)

    def _create_qtree(self, host_id, vol_name, qtree_name):
        """Create a qtree the filer."""
        request = self.client.factory.create('Request')
        request.Name = 'qtree-create'
        qtree_create_xml = (
            '<mode>0755</mode><volume>%s</volume><qtree>%s</qtree>')
        request.Args = text.Raw(qtree_create_xml % (vol_name, qtree_name))
        response = self.client.service.ApiProxy(Target=host_id,
                                                Request=request)
        self._check_fail(request, response)

    def create_snapshot(self, snapshot):
        """Driver entry point for creating a snapshot.

        This driver implements snapshots by using efficient single-file
        (LUN) cloning.
        """
        vol_name = snapshot['volume_name']
        snapshot_name = snapshot['name']
        project = snapshot['project_id']
        lun = self._lookup_lun_for_volume(vol_name, project)
        lun_id = lun.id
        lun = self._get_lun_details(lun_id)
        extra_gb = snapshot['volume_size']
        new_size = '+%dg' % extra_gb
        self._resize_volume(lun.HostId, lun.VolumeName, new_size)
        # LunPath is the partial LUN path in this format: volume/qtree/lun
        lun_path = str(lun.LunPath)
        lun_name = lun_path[lun_path.rfind('/') + 1:]
        qtree_path = '/vol/%s/%s' % (lun.VolumeName, lun.QtreeName)
        src_path = '%s/%s' % (qtree_path, lun_name)
        dest_path = '%s/%s' % (qtree_path, snapshot_name)
        self._clone_lun(lun.HostId, src_path, dest_path, True)

    def delete_snapshot(self, snapshot):
        """Driver entry point for deleting a snapshot."""
        vol_name = snapshot['volume_name']
        snapshot_name = snapshot['name']
        project = snapshot['project_id']
        lun = self._lookup_lun_for_volume(vol_name, project)
        lun_id = lun.id
        lun = self._get_lun_details(lun_id)
        lun_path = '/vol/%s/%s/%s' % (lun.VolumeName, lun.QtreeName,
                                      snapshot_name)
        self._destroy_lun(lun.HostId, lun_path)
        extra_gb = snapshot['volume_size']
        new_size = '-%dg' % extra_gb
        self._resize_volume(lun.HostId, lun.VolumeName, new_size)

    def create_volume_from_snapshot(self, volume, snapshot):
        """Driver entry point for creating a new volume from a snapshot.

        Many would call this "cloning" and in fact we use cloning to implement
        this feature.
        """
        vol_size = volume['size']
        snap_size = snapshot['volume_size']
        if vol_size != snap_size:
            msg = _('Cannot create volume of size %(vol_size)s from '
                'snapshot of size %(snap_size)s')
            raise exception.NovaException(msg % locals())
        vol_name = snapshot['volume_name']
        snapshot_name = snapshot['name']
        project = snapshot['project_id']
        lun = self._lookup_lun_for_volume(vol_name, project)
        lun_id = lun.id
        dataset = lun.dataset
        old_type = dataset.type
        new_type = self._get_ss_type(volume)
        if new_type != old_type:
            msg = _('Cannot create volume of type %(new_type)s from '
                'snapshot of type %(old_type)s')
            raise exception.NovaException(msg % locals())
        lun = self._get_lun_details(lun_id)
        extra_gb = vol_size
        new_size = '+%dg' % extra_gb
        self._resize_volume(lun.HostId, lun.VolumeName, new_size)
        clone_name = volume['name']
        self._create_qtree(lun.HostId, lun.VolumeName, clone_name)
        src_path = '/vol/%s/%s/%s' % (lun.VolumeName, lun.QtreeName,
                                      snapshot_name)
        dest_path = '/vol/%s/%s/%s' % (lun.VolumeName, clone_name, clone_name)
        self._clone_lun(lun.HostId, src_path, dest_path, False)
        self._refresh_dfm_luns(lun.HostId)
        self._discover_dataset_luns(dataset, clone_name)

    def check_for_export(self, context, volume_id):
        raise NotImplementedError()


class NetAppCmodeISCSIDriver(driver.ISCSIDriver):
    """NetApp C-mode iSCSI volume driver."""

    EXEC_WAIT_INTERVAL_SEC =  2
    WORKFLOW_CREATE_CM_LUN        = "Create CM Lun"
    WORKFLOW_MAP_CM_LUN           = "Map CM Lun"
    WORKFLOW_UNMAP_CM_LUN         = "Unmap CM Lun"
    WORKFLOW_REMOVE_CM_LUN        = "Remove CM Lun"
    WORKFLOW_CLONE_CM_LUN         = "Clone CM Lun"
    WORKFLOW_GET_LUN_TARGET_MAP   = "Get lun maping and  iSCSI target details for c-mode cluster,vserver,lun"
    WORKFLOW_READ_LUN_TARGET_MAP  = "Read and clean lun map and CM iscsi target details"
    
    def __init__(self, *args, **kwargs):
        super(NetAppCmodeISCSIDriver, self).__init__(*args, **kwargs)
        self.luns = []
        self.lun_table = {}
        
    def setUp(self, **kwargs):
        self._create_client(**kwargs)
        self._initialise_workflows()
        
    def check_for_setup_error(self):
        pass

    def create_volume(self, volume):
        """ Driver entry point for creating a new volume.
        
            Parameters expected are name, size in mb and os type.
        """
        default_os_type = 'linux'
        default_size = '100'  # 100 MB
        megabytes = 1048576L  
        name = volume['name']
        size = volume['size']
        if size:
            size = int(size)/megabytes
        else:
            size = default_size 
        if hasattr(volume, 'os_type'):
            os_type = volume['os_type']
        else:
            os_type = default_os_type
        self._create_lun(name, os_type, size)   
        
    def delete_volume(self, volume):
        """ Driver entry point for removing a volume """
        name = volume['name']
        location = volume['location']
        self._remove_lun(name, location)

    def ensure_export(self, context, volume):
        pass

    def create_export(self, context, volume):
        pass

    def remove_export(self, context, volume):
        pass

    def initialize_connection(self, volume, connector):
        pass

    def terminate_connection(self, volume, connector):
        pass

    def create_snapshot(self, snapshot):
        """Driver entry point for creating a snapshot.

        This driver implements snapshots by using efficient single-file
        (LUN) cloning.
        """
        vol_name = snapshot['volume_name']
        snapshot_name = snapshot['name']
        location = snapshot['location']
        lun = self._lookup_lun_for_volume(vol_name, location)
        lun_path = str(lun.path)
        lun_name = lun_path[lun_path.rfind('/') + 1:]
        lun_vol_name = self._extract_cm_volume_from_path(lun_path)
        rel_dest_path = snapshot_name
        cluster_name = location.cluster
        vserver_name = location.vserver
        self._clone_lun(cluster_name, vserver_name, lun_vol_name, lun_name, rel_dest_path)

    def delete_snapshot(self, snapshot):
        """Driver entry point for deleting a snapshot."""
        vol_name = snapshot['volume_name']
        snapshot_name = snapshot['name']
        location = snapshot['location']
        lun = self._lookup_lun_for_volume(vol_name, location)
        lun_path = lun.path
        lun_vol_name = self._extract_cm_volume_from_path(lun_path)
        self._destroy_lun(snapshot_name, lun_vol_name, location)
        
    def create_volume_from_snapshot(self, volume, snapshot):
        """Driver entry point for creating a new volume from a snapshot.

        Many would call this "cloning" and in fact we use cloning to implement
        this feature.
        """
        vol_size = volume['size']
        snap_size = snapshot['volume_size']
        if vol_size != snap_size:
            msg = _('Cannot create volume of size %(vol_size)s from '
                'snapshot of size %(snap_size)s')
            raise Exception(msg % locals())
        vol_name = snapshot['volume_name']
        snapshot_name = snapshot['name']
        location = snapshot['location']
        lun = self._lookup_lun_for_volume(vol_name, location)
        lun_path = lun.path
        location = lun.location
        cluster_name = location.cluster
        vserver_name = location.vserver
        lun_vol_name = self._extract_cm_volume_from_path(lun_path)
        clone_name = volume['name']
        self._clone_lun(cluster_name, vserver_name, lun_vol_name, snapshot_name, clone_name)
        

    def check_for_export(self, context, volume_id):
        raise NotImplementedError()
        
    def _create_client(self, **kwargs):
        """ Create client """
        wsdl_url = kwargs['wsdl_url']
        if kwargs['cache']:
            self.client = client.Client(wsdl_url,username=kwargs['login'],password=kwargs['password'])
        else:
            self.client = client.Client(wsdl_url,username=kwargs['login'],password=kwargs['password'],cache=None)
            
    def _initialise_workflows(self):
        """ Map workflow names to the respective ids
        
            The method is used to populate dict with workflow names
            and ids to be used at a later point in the call. Also used
            as method verifying connectivity error.
        """
        workflow_list = self._get_all_workflows()
        self.workflows = dict([(x['name'],x['id']) for x in workflow_list])
        
    def _get_all_workflows(self):
        """ Get all available workflows """
        
        server = self.client.service
        try:
            workflow_list = server.getAllWorkflows()
            return workflow_list
        except:
            raise 
            
    def _convert_res_to_dict(self, param_list):
        """ Convert list params to dictionary for easy access """
        
        dictionary = {}
        for item in param_list:
            dictionary[item.name] = item.value
        return dictionary
      
    def _create_input_param_list(self,**inputkwargs):
        
        """ Helper method to form input param list
            in the expected format.
        """
        input_param_list = [ x + "=" + inputkwargs[x] for x in inputkwargs.keys() ]
        return input_param_list
    
    def _get_workflow_run_response(self,job_id):
        """ Get the job response for a workflow
            with given id. Wait till the job completes
            or timeout.
        """
        
        server = self.client.service
        try:
            response = server.getJobStatus(job_id)
            while(response['jobStatus'] == "RUNNING" or (response['jobStatus'] == "SCHEDULED" and response['scheduleType'] == "Immediate") or (response['jobStatus'] == "PENDING" and response['scheduleType'] == "Immediate")):
                time.sleep(self.EXEC_WAIT_INTERVAL_SEC)
                response = server.getJobStatus(job_id)
            return response
        except suds.WebFault:
            raise
        
    def _get_workflow_return_parameters(self,job_id):
        """ Get return parameter reference for the job id"""
        
        response = self._get_workflow_run_response(job_id)
        if response['jobStatus'] == "COMPLETED":
            if response.__contains__('returnParameter'):
                return_param_map = response['returnParameter']
                return return_param_map
        elif response['jobStatus'] == "FAILED":
            raise exception.NovaException(response['errorMessage'])
        else:
            raise exception.NovaException("Job did not complete successfully. Status: " + response['jobStatus'])
        
    def _execute_workflow(self,workflow_name,**input_params):
        """ Execute workflow identified by name.
        
            Execute the workflow by resolving workflow_id.
            Create input param list in required format.
            Return result parmeters.
        """
        
        server = self.client.service
        workflow_id = self.workflows[workflow_name]
        input_param_list = self._create_input_param_list(**input_params)
        try:
            job_id = server.executeWorkflow(workflow_id,input_param_list)
            return_params = self._get_workflow_return_parameters(job_id)
            return return_params
        except (suds.WebFault, Exception):
            raise 
        
    def _lookup_lun_for_volume(self, name, location):
        """ lookup lun for given name and location """
        
        if self.lun_table.has_key(name):
            return self.lun_table[name]
        suffix = '/' + name
        for lun in self.luns:
            if lun.path.endswith(suffix):
                if lun.location['cluster'] == location['cluster'] and lun.location['vserver'] == location['vserver']:
                    return lun
                else:
                    continue
        msg = _("No entry for lun in volume %s")
        raise exception.NovaException(msg %name)
      
    def _create_lun(self, lun_name, lun_os_type, lun_size_mb):
        """ Create c mode lun """
        
        input_param_list = {"name":lun_name,"os_type":lun_os_type,"size_mb":lun_size_mb}
        result = self._convert_res_to_dict(self._execute_workflow(self.WORKFLOW_CREATE_CM_LUN,**input_param_list))
        location = CmLocation(result['cluster'],result['vserver'])
        path = result['lun_path']                            
        lun = CmLun(location, path)
        self.luns.append(lun)
        self.lun_table[lun_name] = lun
                    
    def _clone_lun(self,cluster_name,vserver_name,volume_name,lun_name,rel_destination_path="",snapshot_name="",space_reserved="N"):
        """ Clone a given cm lun """
        
        param_map = {"cluster_name":cluster_name,"vserver_name":vserver_name,"volume_name":volume_name,"lun_name":lun_name,"space_reserved":space_reserved}
        if rel_destination_path:
            param_map['rel_destination_path'] = rel_destination_path
        if snapshot_name:
            param_map['snapshot_name'] = snapshot_name
        self._execute_workflow(self.WORKFLOW_CLONE_CM_LUN,**param_map)
        
    def _remove_destroy_lun(self, name, location):
        """ Remove a given cm lun from filer and also from data structures"""
        
        lun = self._lookup_lun_for_volume(name, location)
        volume_name = self._extract_cm_volume_from_path(lun.path)
        self._destroy_lun(name, volume_name, location)
        self.lun_table.pop(name)
        self.luns.remove(lun) 
    
    def _destroy_lun(self, name, volume_name, location):
        """ Remove a given cm lun from filer """
        
        cluster_name = location.cluster
        vserver_name = location.vserver
        force = "Y"
        param_map = {"cluster_name":cluster_name,"vserver_name":vserver_name,"volume_name":volume_name,"lun_name":name,"force":force}
        self._execute_workflow(self.WORKFLOW_REMOVE_CM_LUN,**param_map)       
        
    def _unmap_lun(self,cluster_name,vserver_name,volume_name,lun_name,igroup_name):
        """ Unmap a cm lun from initiator group """
        
        param_map = {"cluster_name":cluster_name,"vserver_name":vserver_name,"volume_name":volume_name,"lun_name":lun_name,"igroup_name":igroup_name}
        self._execute_workflow(self.WORKFLOW_UNMAP_CM_LUN,**param_map)
        
        
    def _map_lun(self,cluster_name,vserver_name,volume_name,lun_name,igroup_name,lun_id="",igroup_os_type="iscsi",igroup_portset="",igroup_protocol="iscsi",initiators=""):
        """ Map a cm lun to initiator
        
            Map lun to given initiator. Create initiator
            if its not already present.
        """
        param_map = {"cluster_name":cluster_name,"vserver_name":vserver_name,"vol_name":volume_name,"lun_name":lun_name,"igroup_name":igroup_name}
        if lun_id:
            param_map['lun_id'] = lun_id
        if igroup_os_type:
            param_map['igroup_os_type'] = igroup_os_type
        if igroup_portset:
            param_map['igroup_portset'] = igroup_portset
        if igroup_os_type:
            param_map['igroup_protocol'] = igroup_protocol
        if initiators:
            param_map['initiators'] = initiators
        self._execute_workflow(self.WORKFLOW_MAP_CM_LUN,**param_map)
        
    def _extract_cm_volume_from_path(self, path):
        """ Extracts volume name from a given path """
          
        path_elements = path.split('/')
        if path_elements[1] == 'vol' and len(path_elements) >= 4:
            return path_elements[2]
        elif len(path_elements) >= 3:
            return path_elements[1] # 'vol' not present as prefix   
        msg =_("CM volume not found in path %s")
        raise exception.NovaException(msg %path)  
    
    def _get_lun_iscsi_target_map(self, cluster_name, vserver_name, volume_name, lun_name):
        """ Gets the lun mapping and iscsi target details in WFA """
        param_map = {"cluster_name":cluster_name,"vserver_name":vserver_name,"volume_name":volume_name,"lun_name":lun_name}
        self._execute_workflow(self.WORKFLOW_GET_LUN_TARGET_MAP,**param_map)  
        
    def _read_lun_iscsi_target_map(self, cluster_name, vserver_name, volume_name, lun_name):
        """ Reads the lun mapping and terget details from WFA """
        param_map = {"cluster_name":cluster_name,"vserver_name":vserver_name,"volume_name":volume_name,"lun_name":lun_name}
        return_param = self._execute_workflow(self.WORKFLOW_READ_LUN_TARGET_MAP,**param_map)    



class CmLocation(object):
    """ Represents a location in c mode storage system
    
        Any location in c mode system is a unique combination
        of cluster and vserver.
    """
    
    def __init__(self, cluster, vserver):
        """ cluster is cluster name.
            vserver is vserver name
        """
        
        self.cluster = cluster
        self.vserver = vserver

class CmLun(object):
    """ Represents a c mode lun """
    
    def __init__(self, location, lun_path):
        self.location = location
        self.path = lun_path