import random
import string
import time
import subprocess
from .cfgvars import cfgvars
from .log import get_logger
import os
import re

logger = get_logger(__name__)


def randomstr(leng=4):
    return ''.join(random.choice(string.ascii_uppercase + string.ascii_lowercase + string.digits) for _ in range(leng))


def warn_dependencies():
    required_bins = ["xfreerdp", "update-desktop-database", "mount", "virsh", "pkexec"]
    for depend in required_bins:
        path = os.popen("which " + depend).read().strip()
        if path == "":
            logger.warning("Missing dependency '%s', may cause issues during runtime")


def ip_by_vm_name(name):
    out = os.popen("virsh domifaddr {}".format(name)).read().strip().split("\n")
    if out != [""]:
        try:
            ip = out[2].strip().split()[3].split("/")[0]
            return ip
        except IndexError:
            logger.debug("Vm exists but ip could not be fetched Maybe vm is running in user session"
                         " which is not supported !. Vm name: %s", name)
            return None

    else:
        logger.debug("Cannot find vm by name using virsh. Vm name: %s", name)
        return None


def replace_vars(inp_string):
    values = {
        "!@WINSHAREIP@!": cfgvars.config["host"],
        "!@WINSHAREPORT@!": cfgvars.config["port"],
        "!@WINHOSTNAME@!": cfgvars.config["winvm_hostname"],
        "!@WINUSERNAME@!": cfgvars.config["winvm_username"],
        "!@WINSHAREMOUNTROOT@!": cfgvars.config["winshare_mount_root"],
    }
    for value in values:
        if value in inp_string:
            inp_string = inp_string.replace(value, values[value])
    return inp_string


def create_request(command, message_id=None):
    if type(command) is not list:
        if type(command) is dict:
            command = list(command)
        command = str(command).split(" ")
    if message_id is None:
        message_id = randomstr()
    return {
        "id": message_id,
        "type": "request",
        "command": command
    }


def get_windows_cifs_locations():
    cmd_out = os.popen("mount -t cifs").read().strip().split("\n")
    output = {}
    if cmd_out == [""]:
        return output
    for line in cmd_out:
        net_loc = line[0:line.find(" on /")].strip()
        output[line[len(net_loc + " on "):line.find(" type cifs (")].strip()] = net_loc.replace("/", "\\")
    return output


def mount_pending():
    mount = ""
    uid = os.popen("id -u").read().strip()
    gid = os.popen("id -g").read().strip()
    swapped_mounted_locations = dict((value, key) for key, value in get_windows_cifs_locations().items())
    expanded_cached_shares = var_expanded_shares()
    for resource_path in expanded_cached_shares:
        if expanded_cached_shares[resource_path][0] not in swapped_mounted_locations:
            # This network share is not mounted, create a command to create directory to mount it and command to
            # actually mount it
            mount_point = os.path.join(cfgvars.config["winshare_mount_root"], expanded_cached_shares[resource_path][1])
            mount = mount + 'mkdir -p {mount_pt} && mount -t cifs -o username="{win_uname}",' \
                            'password="{win_pass}",uid={uid},gid={gid} "{net_loc}" "{mount_pt}" && '.format(
                win_uname=cfgvars.config["winvm_username"], win_pass=cfgvars.config["winvm_password"],
                mount_pt=mount_point, net_loc=expanded_cached_shares[resource_path][0], uid=uid, gid=gid
            )
    mount = mount[:-4]
    if mount != "":
        logger.debug("Generated mount command: '%s'", mount)
        if os.environ.get("DIALOG_MODE") == "console":
            logger.debug("Dialog mode set to console.. Using xterm to mount remote drives")
            os.popen("xterm -T 'casualRDH | Enter sudo password to mount drives' -e sudo sh -c '{command}'".format(
                command=mount
            ).replace("\\", "/")).read()
        else:
            os.popen("pkexec sh -c '{command}'".format(command=mount).replace("\\", "/")).read()


def unmount_all():
    swapped_mounted_locations = dict((value, key) for key, value in get_windows_cifs_locations().items())
    expanded_cached_shares = var_expanded_shares()
    umount_cmd = ""
    for resource_path in expanded_cached_shares:
        if expanded_cached_shares[resource_path][0] in swapped_mounted_locations:
            mount_pt = swapped_mounted_locations[expanded_cached_shares[resource_path][0]]
            umount_cmd = umount_cmd + "umount '{mount_pt}' && ".format(mount_pt=mount_pt)
    umount_cmd = umount_cmd[:-4]
    if umount_cmd != "":
        logger.debug("Genetared unmount command: '%s'", umount_cmd)
        if os.environ.get("DIALOG_MODE") == "console":
            logger.debug("Dialog mode set to console.. Using xterm to unmount remote drives")
            os.popen("xterm -T 'casualRDH | Enter sudo password to unmount drives' -e sudo sh -c '{command}'".format(
                command=umount_cmd
            ).replace("\\", "/")).read()
        else:
            os.popen("pkexec sh -c '{command}'".format(command=umount_cmd))


def var_expanded_shares():
    x = cfgvars.config["cached_drive_shares"]
    for resource in x:
        x[resource][0] = replace_vars(x[resource][0])
    return x


def handle_win_ip_paths(path, attempts=2):
    if "!@WINSHAREIP@!" in path:
        attempt = 1
        path_found = False
        while attempt <= attempts:
            expanded_path = replace_vars(path)
            mounted = dict((value, key) for key, value in get_windows_cifs_locations().items())
            print(mounted, get_windows_cifs_locations())
            # First check if it is mounted
            for net_loc in mounted:
                print(net_loc.replace("\\", "/"), expanded_path)
                # (//192.x.x.x/c)/user/somefile.txt
                #     ^----(/mnt/casualrdh/c)/user/somefile.txt
                if expanded_path.startswith(net_loc.replace("\\", "/")):
                    print(expanded_path.startswith(net_loc.replace("\\", "/")))
                    # The location is mounted,
                    path = expanded_path.replace(net_loc.replace("\\", "/"), mounted[net_loc])
                    path_found = True
                    break
            # If it is not try mounting, since we cannot know if polkit has completed
            # wait 10 seconds and check if it it mounted again
            # If mounted ok, else return failure
            if not path_found:
                mount_pending()
                attempt = attempt + 1
            else:
                break
        # It is not a path or a valid path
        if path_found:
            return True, path
        else:
            return False, path
    else:
        return None, path


# Return {"/mnt/casual/d": "\\192.168.40.11\d"}
# {"C:\": ["\\192.168.40.11\c", "c"]}


def path_translate_to_guest(path):
    # If the path exists translate the path, else return the input untouched
    full_path = os.path.abspath(path)
    if os.path.exists(full_path):
        cached_shares = var_expanded_shares()
        mounts = get_windows_cifs_locations()
        # Check if the file being accessed is a mounted windows share
        for mount in mounts:
            if full_path.startswith(mount):
                # This is a mounted windows location, return windows native location, instead of path mapped back to Z:|
                for resource in cached_shares:
                    if cached_shares[resource][0] == mounts[mount]:  # mounts[mount] is net location of mount point
                        if resource.endswith("\\") and full_path != mount:
                            resource = resource[:-1]
                        # resource is windows location of net share
                        return full_path.replace(mount, resource).replace("/", "\\")
        # It is not a windows location, I expect root '/' to be always mounted as Z:\ so ....
        return ("Z:" + full_path).replace("/", "\\")
    else:
        return path


def create_reply(message, data, status):
    message["type"] = "response"
    message["status"] = 1 if status else 0
    message["data"] = data
    return message

def full_rdp():
    command = '{rdc} /d:"{domain}" /u:"{user}" /p:"{passd}" /v:{ip} /a:drive,root,/ +auto-reconnect +clipboard '\
              '/cert-ignore /audio-mode:1 /scale:{scale} /wm-class:"cassowaryApp-FULLSESSION" /dynamic-resolution' \
              ' /{mflag} {rdflag} 1> /dev/null 2>&1 &'
    multimon_enable = int(os.environ.get("RDP_MULTIMON", cfgvars.config["rdp_multimon"]))
    cmd_final = command.format(
        rdflag=cfgvars.config["rdp_flags"],
        domain=cfgvars.config["winvm_hostname"],
        user=cfgvars.config["winvm_username"],
        passd=cfgvars.config["winvm_password"],
        ip=cfgvars.config["host"],
        scale=cfgvars.config["rdp_scale"],
        rdc = cfgvars.config["full_session_client"],
        mflag="multimon" if multimon_enable else "span"
    )
    logger.debug("Creating a full RDP session with commandline  : " + command)
    process = subprocess.Popen(["sh", "-c", "{}".format(cmd_final)])
    process.wait()
    logger.debug("Full RDP session ended !")

def vm_suspension_handler():
    logger.debug("VM watcher active !")
    logger.debug("VM suspend on inactivity is "+"enabled" if bool(cfgvars.config["vm_auto_suspend"]) else "disabled")
    last_active_on = int(time.time()) # Should at least wait for one timeout
    tc = 0
    while True:
        if bool(cfgvars.config["vm_auto_suspend"]):
            vm_suspend_file = "/tmp/cassowary-vm-state-suspend.state"
            process = subprocess.check_output(["ps", "auxfww"])
            # Check if any cassowary started freerdp process is running or not
            print("Seconds of inactivity:", int(time.time()) - last_active_on, "Will sleep after :", cfgvars.config["vm_suspend_delay"])
            if len(re.findall(r"freerdp.*\/wm-class:.*cassowaryApp", process.decode())) >= 1:
                last_active_on = int(time.time())  # Process exists, set last active to current time and do nothing else
            elif int(time.time()) - last_active_on > cfgvars.config["vm_suspend_delay"] \
                        and not os.path.isfile(vm_suspend_file):
                # No cassowary process is running. The VM was relaunched (cassowary should remove this file if any app
                # are run through it), and inactivity time is >= required, so put it to sleep
                logger.debug("Suspending VM due to inactivity !")
                process = subprocess.check_output(["virsh", "suspend", cfgvars.config["vm_name"]])
                logger.debug("VM suspended due to inactivity: "+process.decode())
                # We also Checked if the vm suspend file exists, if it exists that means we previously suspended VM due to
                # inactivity and vm was not resumed by cassowary ! As user may be using VM directly through virt-manager
                # , which we don't want to suspend that session, next suspension happens after next cassowary usage
                logger.debug("Creating suspension marker file")
                open(vm_suspend_file, "w").write("vm-suspended-at-"+str(time.time()))
            # Else, either the VM was suspended and no cassowary application has been launched since then, or we do not
            # have required inactivity duration, do nothing just wait
        time.sleep(2)
        if tc >= 10:
            tc = 0
            logger.debug("Refreshing config to update to probable config changes !")
            cfgvars.refresh_config()
    logger.debug("VM watcher has exited  !")