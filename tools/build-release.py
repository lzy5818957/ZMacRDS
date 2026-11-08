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

VERSION = '0.3.2'
ROOT = pathlib.Path(__file__).resolve().parents[1]
BASE = pathlib.Path('Library/Application Support/ZMacRDS')
WEB_BINARIES = ('zmacrds-web', 'zmacrds-web-source', 'zmacrds-web-encode')
WEB_VERSION = '0.4.1'
MACH_O_HEADERS = {bytes.fromhex(header) for header in
                  ('feedface', 'cefaedfe', 'feedfacf', 'cffaedfe', 'cafebabe', 'bebafeca', 'cafebabf', 'bfbafeca')}


def run(*args):
    subprocess.run([str(arg) for arg in args], check=True)


def sign(path, identity, identifier=None, hardened=False):
    options = ['--timestamp', '--options', 'runtime'] if hardened else ['--timestamp=none']
    if identifier:
        options += ['--identifier', identifier]
    run('/usr/bin/codesign', '--force', '--sign', identity, *options, path)


def sign_runtime(runtime, identity):
    for path in sorted(runtime.rglob('*')):
        if path.is_symlink() or not path.is_file():
            continue
        with path.open('rb') as file:
            if file.read(4) not in MACH_O_HEADERS:
                continue
        sign(path, identity, hardened=True)


def notarize(package, profile):
    credentials = ['--keychain-profile', profile] if profile else [
        '--key', os.environ['ASC_KEY_PATH'], '--key-id', os.environ['ASC_KEY_ID'],
        '--issuer', os.environ['ASC_ISSUER_ID']]
    response = json.loads(subprocess.check_output([
        'xcrun', 'notarytool', 'submit', str(package), *credentials,
        '--wait', '--timeout', '12m', '--output-format', 'json'], text=True))
    if response.get('status') != 'Accepted':
        run('xcrun', 'notarytool', 'log', response['id'], *credentials)
        raise ValueError('Apple did not accept the installer for notarization')
    run('xcrun', 'stapler', 'staple', package)
    run('xcrun', 'stapler', 'validate', package)
    run('/usr/sbin/spctl', '-a', '-t', 'install', '-v', package)


def quick_start(version, notarized):
    if version != VERSION:
        signing = ('安装包已用 Developer ID 签名并经过 Apple 公证。' if notarized
                   else '此构建仅供内部开发测试，未经过 Apple 公证。')
        return (ROOT/'docs/WEB_QUICK_START.html').read_text().replace('@VERSION@', version).replace('@SIGNING@', signing)
    html = (ROOT/'docs/QUICK_START.html').read_text().replace(VERSION, version)
    if notarized:
        html = html.replace('<p>安装包为内部交付，应用使用开发签名，尚未进行 Developer ID 公证。通过交付的 SHA-256 文件核对内容。</p>',
                            '<p>安装包已用 Developer ID 签名并经过 Apple 公证。可通过交付的 SHA-256 文件核对内容。</p>')
        html = html.replace('当前内部包未公证；若系统拦截，在“系统设置 → 隐私与安全”核对文件后点该安装包的“仍要打开”。无需关闭全局安全检查。', '')
    return html


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


def web_licenses(destination):
    cwd = ROOT/'tools/web-desktop/rtc'
    lines = subprocess.check_output(['go', 'list', '-mod=readonly', '-deps', '-f',
        '{{with .Module}}{{if not .Main}}{{.Path}}@{{.Version}}\t{{.Dir}}{{end}}{{end}}', '.'], cwd=cwd, text=True)
    for line in sorted(set(filter(None, lines.splitlines()))):
        name, path = line.split('\t', 1)
        if not path:
            raise ValueError('Go module license directory is missing: '+name)
        source = pathlib.Path(path).resolve()
        target = destination/re.sub(r'[^A-Za-z0-9_.+-]', '_', name)
        files = [p for p in source.iterdir() if p.is_file() and not p.is_symlink() and
                 re.match(r'(?i)^(licen[cs]e|copying|copyright|notice|patents|authors)', p.name)]
        if not files:
            raise ValueError('Go module license is missing: '+name)
        target.mkdir(parents=True, exist_ok=True)
        for license in files:
            shutil.copyfile(license, target/license.name)
    go_root = pathlib.Path(subprocess.check_output(['go', 'env', 'GOROOT'], cwd=cwd, text=True).strip())
    for name in ('LICENSE', 'PATENTS'):
        shutil.copyfile(go_root/name, destination/('Go-'+name))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--runtime', type=pathlib.Path, required=True)
    parser.add_argument('--sign-identity', required=True)
    parser.add_argument('--installer-sign-identity', default='')
    parser.add_argument('--web-pilot', action='store_true')
    parser.add_argument('--notarize', action='store_true', help='Sign with Hardened Runtime, submit to Apple, and staple the ticket.')
    parser.add_argument('--notary-profile', help='Keychain profile; otherwise use ASC_KEY_PATH, ASC_KEY_ID and ASC_ISSUER_ID.')
    args = parser.parse_args()
    if args.notarize and (not args.sign_identity.startswith('Developer ID Application:') or
                          not args.installer_sign_identity.startswith('Developer ID Installer:')):
        parser.error('--notarize requires Developer ID Application and Developer ID Installer identities')
    if args.notarize and not args.notary_profile and not all(os.environ.get(key) for key in
            ('ASC_KEY_PATH', 'ASC_KEY_ID', 'ASC_ISSUER_ID')):
        parser.error('--notarize requires --notary-profile or App Store Connect credentials in the environment')
    if args.notary_profile and not args.notarize:
        parser.error('--notary-profile requires --notarize')
    version = WEB_VERSION if args.web_pilot else VERSION
    guide = quick_start(version, args.notarize)
    run(args.runtime/'bin/python3.13', '-I', '-c', 'import ssl, pathlib, plistlib, hashlib')
    output = ROOT/'build'/('ZMacRDS-'+version+'-arm64'+('-web-pilot' if args.web_pilot else '')+
                            ('-notarized' if args.notarize else ''))
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
            if args.web_pilot:
                info = applications/name/'Contents/Info.plist'
                values = plistlib.loads(info.read_bytes())
                values['CFBundleShortVersionString'] = version
                values['CFBundleVersion'] = version
                info.write_bytes(plistlib.dumps(values))
        if args.web_pilot or args.notarize:
            sign(applications/'ZMacRDS Desktop.app', args.sign_identity, 'org.zmacrds.desktop', args.notarize)
        manager=applications/'ZMacRDS Manager.app'
        app_resources=manager/'Contents/Resources';app_resources.mkdir(exist_ok=True)
        thirdparty=app_resources/'ThirdParty';thirdparty.mkdir();licenses(thirdparty)
        if args.web_pilot: web_licenses(thirdparty/'web')
        shutil.copytree(thirdparty, output/'ThirdPartyLicenses')
        (app_resources/'QuickStart.html').write_text(guide)
        sign(manager, args.sign_identity, 'org.zmacrds.manager', args.notarize)
        sources={'zmacrds-server':ROOT/'build/cargo/release/zmacrds-rdp-probe',
                 'zmacrds-control':ROOT/'build/zmacrds-control',
                 'zmacrds-start-desktop':ROOT/'tools/start-desktop',
                 'zmacrds-configure.py':ROOT/'tools/configure-service.py',
                 'zmacrds-install-service.py':ROOT/'tools/install-service.py',
                 'zmacrds-uninstall.py':ROOT/'tools/uninstall-service.py'}
        if args.web_pilot:
            sources.update({name:ROOT/'build/web-desktop'/name for name in WEB_BINARIES})
            sources['zmacrds-web-admin.py'] = ROOT/'tools/web-lan-admin.py'
            sources['zmacrds-managed-apple.py'] = ROOT/'tools/managed-apple.py'
            sources['zmacrds-managed-screen.py'] = ROOT/'tools/managed-screen.py'
            sources['zmacrds-vnc-gateway.py'] = ROOT/'tools/vnc_gateway.py'
        for name,source in sources.items():
            shutil.copyfile(source, libexec/name);(libexec/name).chmod(0o755 if not name.endswith('.py') else 0o644)
        sign(libexec/'zmacrds-server', args.sign_identity, 'org.zmacrds.server', args.notarize)
        if args.notarize:
            sign(libexec/'zmacrds-control', args.sign_identity, 'org.zmacrds.control', True)
        if args.web_pilot:
            for name in WEB_BINARIES:
                sign(libexec/name, args.sign_identity, 'org.zmacrds.'+name, args.notarize)
        runtime=payload/BASE/'Runtime';shutil.copytree(args.runtime, runtime, symlinks=True)
        for path in runtime.rglob('*'):
            if path.is_symlink():
                if not path.resolve().is_relative_to(runtime.resolve()): raise ValueError('external runtime link')
            else: path.chmod(path.stat().st_mode & 0o777 & ~0o022)
        if args.notarize:
            sign_runtime(runtime, args.sign_identity)
            run(runtime/'bin/python3.13', '-I', '-B', '-c', 'import ssl, ctypes, cryptography.hazmat.bindings._rust')
        paths=['Applications/ZMacRDS Desktop.app/Contents/MacOS/M1Agent',
               'Applications/ZMacRDS Manager.app/Contents/MacOS/ZMacRDSManager']+['usr/local/libexec/'+name for name in sources]
        manifest={'version':version,'architecture':'arm64','files':{'/'+name:hashlib.sha256((payload/name).read_bytes()).hexdigest() for name in paths}}
        if args.web_pilot: manifest['web_pilot'] = True
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
            '--identifier','org.zmacrds.suite','--version',version,'--install-location','/','--ownership','recommended',component)
        requirements=stage/'requirements.plist';requirements.write_bytes(plistlib.dumps({'arch':['arm64'],'os':['14.2'],'home':False}))
        distribution=stage/'Distribution.xml'
        run('/usr/bin/productbuild','--synthesize','--product',requirements,'--package',component,distribution)
        tree=ET.parse(distribution);document=tree.getroot();ET.SubElement(document,'title').text='ZMacRDS '+version+(' Web Pilot' if args.web_pilot else '')
        (resources/'QuickStart.html').write_text(guide)
        ET.SubElement(document,'readme',{'file':'QuickStart.html','mime-type':'text/html'})
        options=document.find('options')
        if options is not None:options.set('customize','never');options.set('rootVolumeOnly','true')
        distribution.write_bytes(ET.tostring(document,encoding='utf-8',xml_declaration=True))
        signing = ['--sign', args.installer_sign_identity] if args.installer_sign_identity else []
        package = output/('ZMacRDS-'+version+'-arm64.pkg')
        run('/usr/bin/productbuild','--distribution',distribution,'--resources',resources,'--package-path',stage,
            *signing, package)
        if args.notarize:
            notarize(package, args.notary_profile)
    (output/'使用说明.html').write_text(guide)
    if not args.web_pilot:
        (output/'RDP-connection-template.rdp').write_text((ROOT/'config/ZMacRDS-fixed.rdp').read_text().replace('127.0.0.1:3390','YOUR_SERVER_IP'))
    uninstall=output/'卸载 ZMacRDS.command'
    uninstall.write_text('#!/bin/sh\nset -eu\n[ -f /usr/local/libexec/zmacrds-uninstall.py ] || { printf "ZMacRDS 已卸载。\\n"; exit 0; }\nprintf "卸载会停止 ZMacRDS 并移除应用，保留本地账号、文档和连接设置。输入 YES 继续："\nread -r answer\n[ "$answer" = YES ] || exit 0\nsudo "/Library/Application Support/ZMacRDS/Runtime/bin/python3.13" -I -B /usr/local/libexec/zmacrds-uninstall.py\nprintf "\\n已完成。按回车关闭。"\nread -r answer\n')
    uninstall.chmod(0o755)
    if not args.web_pilot:
        reports = output/'测试记录'
        reports.mkdir()
        for name in ('LAN_LOCAL_ACCOUNTS_2026-11-06.md', 'RELEASE_0_3_0_2026-11-06.md',
                     'BACKGROUND_MANAGER_2026-11-05.md', 'AUDIO_SERVICE_2026-11-05.md'):
            shutil.copyfile(ROOT/'docs/reports'/name, reports/name)
    hashes=[]
    for path in sorted(output.rglob('*')):
        if path.is_file():hashes.append(hashlib.sha256(path.read_bytes()).hexdigest()+'  '+str(path.relative_to(output)))
    (output/'SHA256SUMS.txt').write_text('\n'.join(hashes)+'\n')
    # Use standard UTF-8 ZIP names so the Chinese guide and test records also
    # extract under their original names with Windows' built-in ZIP reader.
    archive=pathlib.Path(shutil.make_archive(str(output),'zip',root_dir=output.parent,base_dir=output.name))
    print('Delivery:',archive)


if __name__ == '__main__':main()
