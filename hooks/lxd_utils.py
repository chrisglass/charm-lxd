import glob
import io
import json
import pwd
import os
import shutil
from subprocess import call, check_call, check_output, CalledProcessError
import subprocess
import tarfile
import tempfile
import uuid

from charmhelpers.core.templating import render
from charmhelpers.core.hookenv import (
    log,
    config,
    ERROR,
    INFO,
    status_set,
)
from charmhelpers.core.unitdata import kv
from charmhelpers.core.host import (
    add_group,
    add_user_to_group,
    mkdir,
    mount,
    mounts,
    umount,
    service_stop,
    service_start,
    pwgen,
    lsb_release,
)
from charmhelpers.contrib.storage.linux.utils import (
    is_block_device,
    zap_disk,
)
from charmhelpers.contrib.storage.linux.loopback import (
    ensure_loopback_device
)
from charmhelpers.contrib.storage.linux.lvm import (
    create_lvm_volume_group,
    create_lvm_physical_volume,
    list_lvm_volume_group,
    is_lvm_physical_volume,
    deactivate_lvm_volume_group,
    remove_lvm_physical_volume,
)
from charmhelpers.core.decorators import retry_on_exception

BASE_PACKAGES = [
    'btrfs-tools',
    'lvm2',
    'thin-provisioning-tools',
    'criu'
]
LXD_PACKAGES = ['lxd', 'lxd-client']
LXD_SOURCE_PACKAGES = [
    'lxc',
    'lxc-dev',
    'mercurial',
    'git',
    'pkg-config',
    'protobuf-compiler',
    'golang-goprotobuf-dev',
    'build-essential',
    'golang',
    'xz-utils',
    'tar',
    'acl',
]

LXD_GIT = 'github.com/lxc/lxd'
DEFAULT_LOOPBACK_SIZE = '10G'
PW_LENGTH = 16

# The volume group name to use for LXD.
LXD_VOLUME_GROUP = "lxd_vg"

def install_lxd():
    '''Install LXD'''


def install_lxd_source(user='ubuntu'):
    '''Install LXD from source repositories; installs toolchain first'''
    log('Installing LXD from source')

    home = pwd.getpwnam(user).pw_dir
    GOPATH = os.path.join(home, 'go')
    LXD_SRC = os.path.join(GOPATH, 'src', 'github.com/lxc/lxd')

    if not os.path.exists(GOPATH):
        mkdir(GOPATH)

    env = os.environ.copy()
    env['GOPATH'] = GOPATH
    env['HTTP_PROXY'] = 'http://squid.internal:3128'
    env['HTTPS_PROXY'] = 'https://squid.internal:3128'
    cmd = 'go get -v %s' % LXD_GIT
    log('Installing LXD: %s' % (cmd))
    check_call(cmd, env=env, shell=True)

    if not os.path.exists(LXD_SRC):
        log('Failed to go get %s' % LXD_GIT, level=ERROR)
        raise

    cwd = os.getcwd()
    try:
        os.chdir(LXD_SRC)
        cmd = 'go get -v -d ./...'
        log('Downloading LXD deps: %s' % (cmd))
        call(cmd, env=env, shell=True)

        # build deps
        cmd = 'make'
        log('Building LXD deps: %s' % (cmd))
        call(cmd, env=env, shell=True)
    except Exception:
        log("failed to install lxd")
        raise
    finally:
        os.chdir(cwd)


def configure_lxd_source(user='ubuntu'):
    '''Add required configuration and files when deploying LXD from source'''
    log('Configuring LXD Source')
    home = pwd.getpwnam(user).pw_dir
    GOPATH = os.path.join(home, 'go')

    templates_dir = 'templates'
    render('lxd_upstart', '/etc/init/lxd.conf', {},
           perms=0o644, templates_dir=templates_dir)
    render('lxd_service', '/lib/systemd/system/lxd.service', {},
           perms=0o644, templates_dir=templates_dir)
    add_group('lxd', system_group=True)
    add_user_to_group(user, 'lxd')

    service_stop('lxd')
    files = glob.glob('%s/bin/*' % GOPATH)
    for i in files:
        cmd = ['cp', i, '/usr/bin']
        check_call(cmd)
    service_start('lxd')


def configure_lxd_block():
    '''Configure a block device for use by LXD for containers.'''
    log('Configuring LXD container storage')
    if filesystem_mounted('/var/lib/lxd'):
        log('/var/lib/lxd already configured, skipping')
        return

    # Gather all of the config-dependent information first.
    lxd_block_devices = config('block-devices')
    lxd_loopback_device = config("loopback_device")
    lxd_overwrite = config('overwrite')
    lxd_storage_type = config('storage-type')

    # From this point we have a valid list of devices in "devices",
    # irrespective of whether they come from a loopback or real block devices.
    devices = _get_devices_set(lxd_block_devices, lxd_loopback_device)
    if not devices:
        log('Block device or loopback not provided - skipping. LXD will use '
            'the filesystem\'s /var/lib/lxd/ for storage.')
        return

    # NOTE: check overwrite and ensure its only executed once (this code is run
    # on i.e., config-changed
    db = kv()
    if lxd_overwrite and not db.get('scrubbed', False):
        for dev in devices:
            # If we were told to overwrite, zap all block devices.
            clean_storage(dev)
        db.set('scrubbed', True)
        db.flush()

    if not os.path.exists('/var/lib/lxd'):
        mkdir('/var/lib/lxd')

    if lxd_storage_type == 'btrfs':
        _configure_btrfs(devices)
    elif lxd_storage_type == 'lvm':
        _configure_lvm(devices)
    else:
        log("No storage type specified! Using whatever is in /var/lib/lxd for "
            "storage.")


def _configure_loopback_device(device,
                               log=log,
                               ensure_loopback=ensure_loopback_device):
    """Create a loopback device at the path provided.
    """
    log('Configuring loopback device {} for use with LXD'.format(device))
    size = DEFAULT_LOOPBACK_SIZE

    _bd = device.split('|')
    if len(_bd) == 2:
        device, size = _bd
    ensure_loopback(device, size)


def _get_devices_set(block_devices, loopback_device,
                     is_block_device=is_block_device,
                     configure_loopback=_configure_loopback_device):
    """Get a set of valid devices to use for storage.

    This function returns a set() of block devices we can use for storage, no
    matter if they come from the list of block storage devices passed to the
    charm or if they have been built using a loopback devices.

    The returned devices are guaranteed to exist.
    """
    devices = set()  # We might be passed duplicates by error.

    if not block_devices and not loopback_device:
        return devices  # return an empty set

    if block_devices:
        list_of_devices = block_devices.split(" ")
        # Filter out devices that are not found or not a block device.
        for dev in list_of_devices:
            if is_block_device(dev):
                devices.add(dev)
            else:
                log("Provided block device {} invalid or not found. "
                    "Skipping.".format(dev))

    if loopback_device:
        configure_loopback(loopback_device)
        devices.add(loopback_device)
    return devices


def _configure_btrfs(devices, check_call=check_call):
    """Take the necessary steps to let LXD use btrfs as storage backend.
    """
    status_set('maintenance', 'Configuring btrfs container storage')
    service_stop('lxd')

    cmd = ['mkfs.btrfs', '-f'] + list(devices)

    check_call(cmd)
    mount(devices[0],  # Any one of the devices can be used to mount.
            '/var/lib/lxd',
            options='user_subvol_rm_allowed',
            persist=True,
            filesystem='btrfs')

    cmd = ['btrfs', 'quota', 'enable', '/var/lib/lxd']
    check_call(cmd)
    service_start('lxd')


def _configure_lvm(devices):
    """Take the necessary steps to let LXD use LVM as storage backend.
    """
    ready_devices = set()

    # Filter out devices that are already an LVM PV, part of the LXD VG
    for device in devices:
        if (is_lvm_physical_volume(device) and
                list_lvm_volume_group(device) == LXD_VOLUME_GROUP):
            log('Device "{}" already configured for LVM/LXD, skipping'
                ''.format(device))
            continue
        ready_devices.add(device)

    if len(ready_devices) == 0:
        # We were passed only LVM physical volumes that already are part of
        # the LXD VG Noop.
        return

    status_set('maintenance',
            'Configuring LVM container storage')

    # Enable and startup lvm2-lvmetad to avoid extra output
    # in lvm2 commands, which confuses lxd.
    cmd = ['systemctl', 'enable', 'lvm2-lvmetad']
    check_call(cmd)
    cmd = ['systemctl', 'start', 'lvm2-lvmetad']
    check_call(cmd)

    # Make all the ready_devices LVM physical volumes.
    for device in ready_devices:
        log("Creating physical LVM volume on {}.".format(device))
        create_lvm_physical_volume(device)

    # Does the volume group already exist? If so, we need to extend it and not
    # create it.
    vg_exists = LXD_VOLUME_GROUP in check_output(["vgs"])
    if not vg_exists:
        # No volume group exists - let's just go ahead and pass all our devices
        # to it.
        # NOTE: The charmhelpers volume group creation helper accepts a single
        # block device. Since it's a very thin wrapper, do it here.
        log("Creating volume group '{}'".format(LXD_VOLUME_GROUP))
        check_call(['vgcreate', LXD_VOLUME_GROUP] + list(devices))
    else:
        # Our volume group already exists, and some more ready disks are to be
        # used as well. Let's vgextend the existing group.
        log("Volume group '{}' already exists.".format(LXD_VOLUME_GROUP))
        for device in ready_devices:
            log("Extending volume group '{}' with {}"
                "".format(LXD_VOLUME_GROUP, device))
            check_call(['vgextend', LXD_VOLUME_GROUP, device])

    # Set the LXD config to use our volume group.
    cmd = ['lxc', 'config', 'set', 'storage.lvm_vg_name', LXD_VOLUME_GROUP]
    check_call(cmd)

    # The LVM thinpool logical volume is lazily created, either on
    # image import or container creation. This will force LV creation.
    create_and_import_busybox_image()


def _configure_loopback_device(device, ensure_loopback=ensure_loopback_device):
    """Create a loopback device at the path provided.
    """
    log('Configuring loopback device {} for use with LXD'.format(device))
    size = DEFAULT_LOOPBACK_SIZE

    _bd = device.split('|')
    if len(_bd) == 2:
        device, size = _bd
    ensure_loopback(device, size)
    return device


def create_and_import_busybox_image():
    """Create a busybox image for lxd.

    This creates a busybox image without reaching out to
    the network.

    This function is, for the most part, heavily based on
    the busybox image generation in the pylxd integration
    tests.
    """
    workdir = tempfile.mkdtemp()
    xz = "xz"

    destination_tar = os.path.join(workdir, "busybox.tar")
    target_tarball = tarfile.open(destination_tar, "w:")

    metadata = {'architecture': os.uname()[4],
                'creation_date': int(os.stat("/bin/busybox").st_ctime),
                'properties': {
                    'os': "Busybox",
                    'architecture': os.uname()[4],
                    'description': "Busybox %s" % os.uname()[4],
                    'name': "busybox-%s" % os.uname()[4],
                    # Don't overwrite actual busybox images.
                    'obfuscate': str(uuid.uuid4)}}

    # Add busybox
    with open("/bin/busybox", "rb") as fd:
        busybox_file = tarfile.TarInfo()
        busybox_file.size = os.stat("/bin/busybox").st_size
        busybox_file.mode = 0o755
        busybox_file.name = "rootfs/bin/busybox"
        target_tarball.addfile(busybox_file, fd)

    # Add symlinks
    busybox = subprocess.Popen(["/bin/busybox", "--list-full"],
                               stdout=subprocess.PIPE,
                               universal_newlines=True)
    busybox.wait()

    for path in busybox.stdout.read().split("\n"):
        if not path.strip():
            continue

        symlink_file = tarfile.TarInfo()
        symlink_file.type = tarfile.SYMTYPE
        symlink_file.linkname = "/bin/busybox"
        symlink_file.name = "rootfs/%s" % path.strip()
        target_tarball.addfile(symlink_file)

    # Add directories
    for path in ("dev", "mnt", "proc", "root", "sys", "tmp"):
        directory_file = tarfile.TarInfo()
        directory_file.type = tarfile.DIRTYPE
        directory_file.name = "rootfs/%s" % path
        target_tarball.addfile(directory_file)

    # Add the metadata file
    metadata_yaml = json.dumps(metadata, sort_keys=True,
                               indent=4, separators=(',', ': '),
                               ensure_ascii=False).encode('utf-8') + b"\n"

    metadata_file = tarfile.TarInfo()
    metadata_file.size = len(metadata_yaml)
    metadata_file.name = "metadata.yaml"
    target_tarball.addfile(metadata_file,
                           io.BytesIO(metadata_yaml))

    inittab = tarfile.TarInfo()
    inittab.size = 1
    inittab.name = "/rootfs/etc/inittab"
    target_tarball.addfile(inittab, io.BytesIO(b"\n"))

    target_tarball.close()

    # Compress the tarball
    r = subprocess.call([xz, "-9", destination_tar])
    if r:
        raise Exception("Failed to compress: %s" % destination_tar)

    image_file = destination_tar+".xz"

    cmd = ['lxc', 'image', 'import', image_file, '--alias', 'busybox']
    check_call(cmd)

    shutil.rmtree(workdir)


def determine_packages():
    packages = [] + BASE_PACKAGES
    packages = list(set(packages))
    if config('use-source'):
        packages.extend(LXD_SOURCE_PACKAGES)
    else:
        packages.extend(LXD_PACKAGES)
    return packages


def filesystem_mounted(fs):
    return fs in [f for f, m in mounts()]


def lxd_trust_password():
    db = kv()
    password = db.get('lxd-password')
    if not password:
        password = db.set('lxd-password', pwgen(PW_LENGTH))
        db.flush()
    return password


def configure_lxd_remote(settings, user='root'):
    cmd = ['sudo', '-u', user,
           'lxc', 'remote', 'list']
    output = check_output(cmd)
    if settings['hostname'] not in output:
        log('Adding new remote {hostname}:{address}'.format(**settings))
        cmd = ['sudo', '-u', user,
               'lxc', 'remote', 'add',
               settings['hostname'],
               'https://{}:8443'.format(settings['address']),
               '--accept-certificate',
               '--password={}'.format(settings['password'])]
        check_call(cmd)
    else:
        log('Updating remote {hostname}:{address}'.format(**settings))
        cmd = ['sudo', '-u', user,
               'lxc', 'remote', 'set-url',
               settings['hostname'],
               'https://{}:8443'.format(settings['address'])]
        check_call(cmd)


@retry_on_exception(5, base_delay=2, exc_type=CalledProcessError)
def configure_lxd_host():
    ubuntu_release = lsb_release()['DISTRIB_CODENAME'].lower()
    if ubuntu_release > "vivid":
        log('>= Wily deployment - configuring LXD trust password and address',
            level=INFO)
        cmd = ['lxc', 'config', 'set',
               'core.trust_password', lxd_trust_password()]
        check_call(cmd)
        cmd = ['lxc', 'config', 'set',
               'core.https_address', '[::]']
        check_call(cmd)
    elif ubuntu_release == "vivid":
        log('Vivid deployment - loading overlay kernel module', level=INFO)
        cmd = ['modprobe', 'overlay']
        check_call(cmd)
        with open('/etc/modules', 'r+') as modules:
            if 'overlay' not in modules.read():
                modules.write('overlay')


def clean_storage(block_device):
    '''Ensures a block device is clean.  That is:
        - unmounted
        - any lvm volume groups are deactivated
        - any lvm physical device signatures removed
        - partition table wiped

    :param block_device: str: Full path to block device to clean.
    '''
    for mp, d in mounts():
        if d == block_device:
            log('clean_storage(): Found %s mounted @ %s, unmounting.' %
                (d, mp))
            umount(mp, persist=True)

    if is_lvm_physical_volume(block_device):
        deactivate_lvm_volume_group(block_device)
        remove_lvm_physical_volume(block_device)

    zap_disk(block_device)


def lxd_running():
    '''Check whether LXD is running or not'''
    cmd = ['pgrep', 'lxd']
    try:
        check_call(cmd)
        return True
    except CalledProcessError:
        return False


def assess_status():
    '''Determine status of current unit'''
    if lxd_running():
        status_set('active', 'Unit is ready')
    else:
        status_set('blocked', 'LXD is not running')
