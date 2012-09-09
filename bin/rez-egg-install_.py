#!!REZ_PYTHON_BINARY!

#
# Install a python egg as a Rez package!
#

import optparse
import sys
import os
import re
import stat
import yaml
import time
import os.path
import shutil
import tempfile
import subprocess as sp


_g_r_stat = stat.S_IRUSR|stat.S_IRGRP|stat.S_IROTH
_g_x_stat = stat.S_IXUSR|stat.S_IXGRP|stat.S_IXOTH

_g_rez_egg_api_version  = 0
_g_rez_path             = os.getenv("REZ_PATH", "UNKNOWN_REZ_PATH")
_g_pkginfo_key_re       = re.compile("^[A-Z][a-z_-]+:")
_g_yaml_prettify_re     = re.compile("^([^: \\n]+):", re.MULTILINE)


# this is because rez doesn't have alphanumeric version support. It will have though, when
# ported to Certus. Just not yet. :(
def _convert_version(txt):
    ver = ''
    for ch in txt:
        num = None
        if ch>='a' and ch<='z':
            num = ord(ch) - ord('a')
        elif ch>='A' and ch<='Z':
            num = ord(ch) - ord('A') + 26
        elif ch=='_':
            num = 26+26
        if num is None:
            ver += ch
        else:
            ver += ".%d." % num

    ver = ver.replace("..",".")
    ver = ver.strip('.')
    return ver


def _convert_pkg_name(name, pkg_remappings):
    name2 = pkg_remappings.get(name)
    if name2:
        name = _convert_pkg_name(name2, {})
    return name.replace('-','_')


def _convert_requirement(req, pkg_remappings):
    pkg_name = _convert_pkg_name(req.project_name, pkg_remappings)
    if not req.specs:
        return [pkg_name]

    rezreqs = []
    for spec in req.specs:
        op,ver = spec
        rezver = _convert_version(ver)
        if op == "<":
            r = "%s-0+<%s" % (pkg_name, rezver)
            rezreqs.append(r)
        elif op == "<=":
            r = "%s-0+<%s|%s" % (pkg_name, rezver, rezver)
            rezreqs.append(r)
        elif op == "==":
            r = "%s-%s" % (pkg_name, rezver)
            rezreqs.append(r)
        elif op == ">=":
            r = "%s-%s+" % (pkg_name, rezver)
            rezreqs.append(r)
        elif op == ">":
            r1 = "%s-%s+" % (pkg_name, rezver)
            r2 = "!%s-%s" % (pkg_name, rezver)
            rezreqs.append(r1)
            rezreqs.append(r2)
        elif op == "!=":
            r = "!%s-%s" % (pkg_name, rezver)
            rezreqs.append(r)
        else:
            raise Exception("Unknown operator '%s'" % op)

    return rezreqs


def _convert_metadata(distr):
    meta = {}
    if distr.has_metadata("PKG-INFO"):
        s = distr.get_metadata("PKG-INFO")
        sections = pkg_r.split_sections(s)
        for section in sections:
            entries = section[1]
            for e in entries:
                if _g_pkginfo_key_re.match(e):
                    toks = e.split(':',1)
                    k = toks[0].strip()
                    v = toks[1].strip()
                    meta[k] = v
    return meta



#########################################################################################
# cmdlin
#########################################################################################
usage = "usage: rez-egg-install [options] <package_name> [-- <easy_install args>]\n\n" + \
    "  Rez-egg-install installs Python eggs as Rez packages, using the standard\n" + \
    "  'easy_install' python module installation tool. For example:\n" + \
    "  rez-egg-install pylint\n" + \
    "  If you need to use specific easy_install options, include the second\n" + \
    "  set of args - in this case you need to make sure that <package_name>\n" + \
    "  matches the egg that you're installing, for example:\n" + \
    "  rez-egg-install MyPackage -- http://somewhere/MyPackage-1.0.tgz\n" + \
    "  Rez will install the package into the current release path, set in\n" + \
    "  $REZ_EGG_PACKAGES_PATH, which is currently:\n" + \
    "  " + (os.getenv("REZ_EGG_PACKAGES_PATH") or "UNSET!")

p = optparse.OptionParser(usage=usage)
p.add_option("--mapping-file", dest="mapping_file", type="str", \
    default="%s/template/egg_remap.yaml" % _g_rez_path, \
    help="yaml file that remaps package names [default = %default]")
p.add_option("--ignore-unknown-platforms", dest="ignore_unknown_plat", \
    action="store_true", default=False, \
    help="skip unknown egg platforms, and install as a rez package with no platform dependencies")
p.add_option("--dry-run", dest="dry_run", action="store_true", default=False, \
    help="perform a dry run [default = %default]")
p.add_option("--local", dest="local", action="store_true", default=False, \
    help="install to local packages directory instead [default = %default]")
p.add_option("--no-clean", dest="no_clean", action="store_true", default=False, \
    help="don't delete temporary egg files afterwards [default = %default]")

help_args = set(["--help","-help","-h","--h"]) & set(sys.argv)
if help_args:
    p.parse_args()

rez_args = None
easy_install_args = None

if "--" in sys.argv:
    i = sys.argv.index("--")
    rez_args = sys.argv[1:i]
    easy_install_args = sys.argv[i+1:]
else:
    rez_args = sys.argv[1:]

(opts, args) = p.parse_args(rez_args)
if len(args) != 1:
    p.error("Expected package name")
pkg_name = args[0]

if not easy_install_args:
    easy_install_args = [pkg_name]

install_evar = "REZ_EGG_PACKAGES_PATH"
if opts.local:
    install_evar = "REZ_LOCAL_PACKAGES_PATH"

install_path = os.getenv(install_evar)
if not install_path:
    print >> sys.stderr, "Expected $%s to be set." % install_evar
    sys.exit(1)


remappings = {}
if opts.mapping_file:
    with open(opts.mapping_file, 'r') as f:
        s = f.read()
    remappings = yaml.load(s)
platform_remappings = remappings.get("platform_mappings") or {}
package_remappings = remappings.get("package_mappings") or {}



#########################################################################################
# run easy_install
#########################################################################################

# find easy_install
proc = sp.Popen("which easy_install", shell=True, stdout=sp.PIPE, stderr=sp.PIPE)
proc.communicate()
if proc.returncode:
    print >> sys.stderr, "could not find easy_install."
    sys.exit(1)

# install the egg to a temp dir
egg_path = tempfile.mkdtemp(prefix="rez-egg-download-")
print "INSTALLING EGG FOR PACKAGE '%s' TO %s..." % (pkg_name, egg_path)

def _clean():
    if not opts.no_clean:
        print
        print "DELETING %s..." % egg_path
        shutil.rmtree(egg_path)

cmd = "export PYTHONPATH=$PYTHONPATH:%s" % egg_path
cmd += " ; easy_install --install-dir=%s %s" % (egg_path, str(' ').join(easy_install_args))
print "Running: %s" % cmd
proc = sp.Popen(cmd, shell=True)
proc.wait()
if proc.returncode:
    _clean()
    print
    print >> sys.stderr, "A problem occurred running easy_install, the command was:\n%s" % cmd
    sys.exit(proc.returncode)



#########################################################################################
# extract info from eggs
#########################################################################################
sys.path = [egg_path] + sys.path
import pkg_resources as pkg_r

distrs = pkg_r.find_distributions(egg_path)
eggs = {}

for distr in distrs:
    print
    print "EXTRACTING DATA FROM %s..." % distr.location

    name = _convert_pkg_name(distr.project_name, package_remappings)
    ver = _convert_version(distr.version)
    pyver = _convert_version(distr.py_version)

    d = {
        "config_version":   0,
        "name":             name,
        "version":          ver,
        "requires":         ["python-%s+" % pyver]
    }

    pkg_d = _convert_metadata(distr)
    d["EGG-INFO"] = pkg_d
    
    v = pkg_d.get("Summary")
    v2 = pkg_d.get("Description")
    if v:
        d["description"] = v
    elif v2:
        d["description"] = v2

    v = pkg_d.get("Author")
    if v:
        d["author"] = v

    v = pkg_d.get("Home-page")
    if v:
        pkg_d["help"] = "$BROWSER %s" % v

    reqs = distr.requires()
    for req in reqs:
        rezreqs = _convert_requirement(req, package_remappings)
        d["requires"] += rezreqs

    v = pkg_d.get("Platform")
    if v:
        platform_pkgs = platform_remappings.get(v)
        if platform_pkgs is not None:
            if platform_pkgs:
                d["variants"] = platform_pkgs
        elif not opts.ignore_unknown_plat:
            print >> sys.stderr, ("No remappings are present for the platform '%s'. " + \
                "Please use the --mapping-file option to provide the remapping, or " + \
                "the --ignore-unknown-platforms option.") % v
            sys.exit(1)

    eggs[name] = (distr, d)
    


#########################################################################################
# convert eggs to rez packages
#########################################################################################
destdirs = []

def _mkdir(path, make_ro=True):
    if not os.path.exists(path):
        print "creating %s..." % path
        if not opts.dry_run:
            os.makedirs(path)
            if make_ro:
                destdirs.append(path)

def _cpfile(filepath, destdir):
    print "copying %s to %s..." % (filepath, destdir+'/')
    if not opts.dry_run:
        shutil.copy(filepath, destdir)
        destfile = os.path.join(destdir, os.path.basename(filepath))
        os.chmod(destfile, _g_r_stat)


for egg_name, v in eggs.iteritems():
    print
    print "BUILDING REZ PACKAGE FOR '%s'..." % egg_name

    variants = d.get("variants") or []
    distr, d = v
    egg_path = distr.location
    egg_dir = os.path.basename(egg_path)
    
    pkg_path = os.path.join(install_path, egg_name, d["version"])
    meta_path = os.path.join(pkg_path, ".metadata")    
    variant_path = os.path.join(pkg_path, *(variants))

    if os.path.exists(variant_path):
        print ("skipping installation of '%s', the current variant appears to exist already " + \
            "- %s already exists. Delete this directory to force a reinstall.") % \
            (egg_name, variant_path)
        continue

    _mkdir(meta_path, False)
    _mkdir(variant_path, bool(variants))

    # copy files
    for root, dirs, files in os.walk(egg_path):
        subpath = root[len(egg_path):].strip('/')
        dest_root = os.path.join(variant_path, egg_dir, subpath)
        _mkdir(dest_root)

        for name in dirs:
            _mkdir(os.path.join(dest_root, name))

        for name in files:
            if not name.endswith(".pyc"):
                _cpfile(os.path.join(root, name), dest_root)

    for path in reversed(destdirs):
        os.chmod(path, _g_r_stat|_g_x_stat)

    # create/update yaml
    print
    pkg_d = {}
    yaml_path = os.path.join(pkg_path, "package.yaml")
    if os.path.exists(yaml_path):
        print "UPDATING %s..." % yaml_path
        with open(yaml_path, 'r') as f:
            s = f.read()
        pkg_d = yaml.load(s) or {}
    else:
        print "CREATING %s..." % yaml_path

    for k,v in d.iteritems():
        if k not in pkg_d:
            pkg_d[k] = v

    if variants:
        if "variants" not in pkg_d:
            pkg_d["variants"] = []
        pkg_d["variants"].append(variants)
    
    if "commands" not in pkg_d:
        pkg_d["commands"] = []
    cmd = "export PYTHONPATH=$PYTHONPATH:!ROOT!/%s" % egg_dir
    if cmd not in pkg_d["commands"]:
        pkg_d["commands"].append(cmd)

    s = yaml.dump(pkg_d, default_flow_style=False)
    pretty_s = re.sub(_g_yaml_prettify_re, "\\n\\1:", s).strip() + '\n'

    if opts.dry_run:
        print
        print "CONTENTS OF %s WOULD BE:" % yaml_path
        print pretty_s
    else:
        with open(yaml_path, 'w') as f:
            f.write(pretty_s)

        # timestamp
        timefile = os.path.join(meta_path, "release_time.txt")
        if not os.path.exists(timefile):
            with open(timefile, 'w') as f:
                f.write(str(int(time.time())))

_clean()



#    Copyright 2008-2012 Dr D Studios Pty Limited (ACN 127 184 954) (Dr. D Studios)
#
#    This file is part of Rez.
#
#    Rez is free software: you can redistribute it and/or modify
#    it under the terms of the GNU Lesser General Public License as published by
#    the Free Software Foundation, either version 3 of the License, or
#    (at your option) any later version.
#
#    Rez is distributed in the hope that it will be useful,
#    but WITHOUT ANY WARRANTY; without even the implied warranty of
#    MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
#    GNU General Public License for more details.
#
#    You should have received a copy of the GNU Lesser General Public License
#    along with Rez.  If not, see <http://www.gnu.org/licenses/>.
