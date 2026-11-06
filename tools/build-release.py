#!/usr/bin/env python3
"""Build a self-contained macOS Installer and a private-data-free delivery folder."""
import argparse
import hashlib
import json
import os
import pathlib
import plistlib
import re
import shutil
import subprocess
import tempfile
import xml.etree.ElementTree as ET

VERSION = '0.3.0'
ROOT = pathlib.Path(__file__).resolve().parents[1]
BASE = pathlib.Path('Library/Application Support/ZMacRDS')


def run(*args):
    subprocess.run([str(arg) for arg in args], check=True)


def licenses(destination):
    metadata = json.loads(subprocess.check_output(['cargo', 'metadata', '--locked', '--format-version', '1'], cwd=ROOT))
    records = []
    for package in metadata['packages']:
        source = pathlib.Path(package['manifest_path']).parent
        label = re.sub(r'[^A-Za-z0-9_.+-]', '_', package['name']+'-'+package['version'])
        selected = [path for path in source.iterdir() if path.is_file() and re.match(r'(?i)^(licen[cs]e|copying|copyright|notice)', path.name)]
        explicit = package.get('license_file')
        if explicit:
            path = pathlib.Path(explicit)
            if not path.is_absolute(): path = source/path
            if path.is_file() and path not in selected: selected.append(path)
        copied = []
        for path in selected:
            # Cargo metadata is local build input, not permission to read
            # arbitrary home files through a package's license reference.
            resolved = path.resolve()
            if not (resolved.is_relative_to(source.resolve()) or resolved == ROOT/'LICENSE'):
                raise ValueError('license outside package: '+package['name'])
            target = destination/label/path.name;target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(path, target);copied.append(label+'/'+path.name)
        records.append({'name': package['name'], 'version': package['version'], 'license': package.get('license'), 'files': copied})
    (destination/'index.json').write_text(json.dumps(records, ensure_ascii=False, indent=2))
    (destination/'README.txt').write_text('Third-party Rust package licenses. Python and its bundled dependency licenses are also retained in the installed Runtime distribution.\n')
    shutil.copyfile(ROOT/'LICENSE', destination/'ZMacRDS-LICENSE')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--runtime', type=pathlib.Path, required=True)
    parser.add_argument('--sign-identity', required=True)
    args = parser.parse_args()
    run(args.runtime/'bin/python3.13', '-I', '-c', 'import ssl, pathlib, plistlib, hashlib')
    output = ROOT/'build'/('ZMacRDS-'+VERSION+'-arm64')
    # This exact build-output folder contains generated distribution artifacts.
    if output.exists(): shutil.rmtree(output)
    output.mkdir()
    with tempfile.TemporaryDirectory(prefix='release-', dir=ROOT/'build') as temporary:
        stage = pathlib.Path(temporary);payload=stage/'payload';scripts=stage/'scripts';resources=stage/'resources'
        scripts.mkdir();resources.mkdir();(payload/BASE).mkdir(parents=True)
        (payload/BASE).chmod(0o700)
        libexec=payload/'usr/local/libexec';libexec.mkdir(parents=True)
        applications=payload/'Applications';applications.mkdir()
        for source,name in [('ZMacRDS-Desktop.app','ZMacRDS Desktop.app'),('ZMacRDS-Manager.app','ZMacRDS Manager.app')]:
            shutil.copytree(ROOT/'build'/source, applications/name, symlinks=True)
        manager=applications/'ZMacRDS Manager.app'
        app_resources=manager/'Contents/Resources';app_resources.mkdir(exist_ok=True)
        thirdparty=app_resources/'ThirdParty';thirdparty.mkdir();licenses(thirdparty)
        shutil.copytree(thirdparty, output/'ThirdPartyLicenses')
        shutil.copyfile(ROOT/'docs/QUICK_START.html', app_resources/'QuickStart.html')
        run('/usr/bin/codesign','--force','--sign',args.sign_identity,'--timestamp=none','--identifier','org.zmacrds.manager',manager)
        sources={'zmacrds-server':ROOT/'build/cargo/release/zmacrds-rdp-probe',
                 'zmacrds-control':ROOT/'build/zmacrds-control',
                 'zmacrds-start-desktop':ROOT/'tools/start-desktop',
                 'zmacrds-configure.py':ROOT/'tools/configure-service.py',
                 'zmacrds-install-service.py':ROOT/'tools/install-service.py',
                 'zmacrds-uninstall.py':ROOT/'tools/uninstall-service.py'}
        for name,source in sources.items():
            shutil.copyfile(source, libexec/name);(libexec/name).chmod(0o755 if not name.endswith('.py') else 0o644)
        runtime=payload/BASE/'Runtime';shutil.copytree(args.runtime, runtime, symlinks=True)
        for path in runtime.rglob('*'):
            if path.is_symlink():
                if not path.resolve().is_relative_to(runtime.resolve()): raise ValueError('external runtime link')
            else: path.chmod(path.stat().st_mode & 0o777 & ~0o022)
        paths=['Applications/ZMacRDS Desktop.app/Contents/MacOS/M1Agent',
               'Applications/ZMacRDS Manager.app/Contents/MacOS/ZMacRDSManager']+['usr/local/libexec/'+name for name in sources]
        manifest={'version':VERSION,'architecture':'arm64','files':{'/'+name:hashlib.sha256((payload/name).read_bytes()).hexdigest() for name in paths}}
        (payload/BASE/'installation.json').write_text(json.dumps(manifest,indent=2))
        shutil.copyfile(ROOT/'tools/installer-preflight', scripts/'preinstall');(scripts/'preinstall').chmod(0o755)
        shutil.copyfile(ROOT/'tools/installer-postflight.py',scripts/'postflight.py')
        (scripts/'postinstall').write_text('#!/bin/sh\nset -eu\n[ "${3:-/}" = / ] || exit 1\nexec "/Library/Application Support/ZMacRDS/Runtime/bin/python3.13" -I -B "$(dirname "$0")/postflight.py"\n')
        (scripts/'postinstall').chmod(0o755)
        components=stage/'components.plist';run('/usr/bin/pkgbuild','--analyze','--root',payload,components)
        items=plistlib.loads(components.read_bytes())
        for item in items:
            item.update(BundleIsRelocatable=False,BundleHasStrictIdentifier=True,BundleIsVersionChecked=True,BundleOverwriteAction='upgrade')
        components.write_bytes(plistlib.dumps(items))
        component=stage/'ZMacRDS-component.pkg'
        run('/usr/bin/pkgbuild','--root',payload,'--component-plist',components,'--scripts',scripts,
            '--identifier','org.zmacrds.suite','--version',VERSION,'--install-location','/','--ownership','recommended',component)
        requirements=stage/'requirements.plist';requirements.write_bytes(plistlib.dumps({'arch':['arm64'],'os':['14.2'],'home':False}))
        distribution=stage/'Distribution.xml'
        run('/usr/bin/productbuild','--synthesize','--product',requirements,'--package',component,distribution)
        tree=ET.parse(distribution);document=tree.getroot();ET.SubElement(document,'title').text='ZMacRDS '+VERSION
        shutil.copyfile(ROOT/'docs/QUICK_START.html',resources/'QuickStart.html')
        ET.SubElement(document,'readme',{'file':'QuickStart.html','mime-type':'text/html'})
        options=document.find('options')
        if options is not None:options.set('customize','never');options.set('rootVolumeOnly','true')
        distribution.write_bytes(ET.tostring(document,encoding='utf-8',xml_declaration=True))
        run('/usr/bin/productbuild','--distribution',distribution,'--resources',resources,'--package-path',stage,output/('ZMacRDS-'+VERSION+'-arm64.pkg'))
    shutil.copyfile(ROOT/'docs/QUICK_START.html',output/'使用说明.html')
    shutil.copyfile(ROOT/'config/ZMacRDS-fixed.rdp',output/'ZMacRDS-fixed.rdp')
    uninstall=output/'卸载 ZMacRDS.command'
    uninstall.write_text('#!/bin/sh\nset -eu\n[ -f /usr/local/libexec/zmacrds-uninstall.py ] || { printf "ZMacRDS 已卸载。\\n"; exit 0; }\nprintf "卸载会停止 ZMacRDS 并移除应用，保留本地账号、文档和连接设置。输入 YES 继续："\nread -r answer\n[ "$answer" = YES ] || exit 0\nsudo "/Library/Application Support/ZMacRDS/Runtime/bin/python3.13" -I -B /usr/local/libexec/zmacrds-uninstall.py\nprintf "\\n已完成。按回车关闭。"\nread -r answer\n')
    uninstall.chmod(0o755)
    reports=output/'测试记录';reports.mkdir()
    for name in ['RELEASE_0_3_0_2026-11-06.md','BACKGROUND_MANAGER_2026-11-05.md','AUDIO_SERVICE_2026-11-05.md']:
        shutil.copyfile(ROOT/'docs/reports'/name,reports/name)
    hashes=[]
    for path in sorted(output.rglob('*')):
        if path.is_file():hashes.append(hashlib.sha256(path.read_bytes()).hexdigest()+'  '+str(path.relative_to(output)))
    (output/'SHA256SUMS.txt').write_text('\n'.join(hashes)+'\n')
    archive=output.parent/(output.name+'.zip');run('/usr/bin/ditto','-c','-k','--sequesterRsrc','--keepParent',output,archive)
    print('Delivery:',archive)


if __name__ == '__main__':main()
