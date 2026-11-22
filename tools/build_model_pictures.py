#!/usr/bin/env python3
"""Build experimental HXtra Model Pictures from a local HX Stomp 3.80.0 HXF."""
import argparse
from concurrent.futures import ThreadPoolExecutor
import hashlib
import json
import os
from pathlib import Path
import re
import struct
import subprocess
import sys
import tempfile
from urllib.request import Request, urlopen
from xml.etree import ElementTree

from PIL import Image
import numpy as np
from scipy import ndimage

ROOT = Path(__file__).resolve().parents[1]
import image_store
import l6ia
import m4asm
import patch_amp as pa
import patch_menu as pm
import setting_ids
from features import model_pictures, real_names

CATALOG = Path(__file__).with_name('model_pictures_art.json')
BUILD_ROOT = ROOT / 'build/model-pictures'
TONE3000 = 'https://www.tone3000.com/'
FEATURE_FLAGS = (
    'BPM', 'BREATHING_LINE', 'CC_TAP', 'CLICK', 'DSP_METER', 'FX_GATE', 'GATE_SC', 'GR',
    'LABEL_COLOR', 'LONG_PRESS', 'MODEL_PICTURES', 'MORE_SNAPSHOTS', 'PRESET_COLOR',
    'REAL_NAMES', 'SCREEN_MIRROR', 'TAP_COLOR', 'TUNER', 'USB_INPUT', 'USB_MONITOR',
)
IMAGE_LIMIT = 8 * 1024 * 1024
# Slots 0–18 follow the firmware's model browser categories.
CATEGORY_STYLES = (
    ('', None), ('', None), ('DIST', '#F2C66D'), ('DYNAMICS', '#E7DE74'),
    ('EQ', '#B9D978'), ('MOD', '#8FD4FF'), ('DELAY', '#58EF70'), ('REVERB', '#FFAD78'),
    ('PITCH', '#C6A0FF'), ('FILTER', '#F28BC4'), ('WAH', '#F28BC4'),
    ('AMP+CAB', '#FF947A'), ('AMP', '#FF947A'), ('PREAMP', '#FF947A'),
    ('CAB', '#FF7373'), ('IR', '#FF7373'), ('VOL/PAN', '#ABB7C5'),
    ('SEND/RTN', '#ABB7C5'), ('LOOPER', '#ABB7C5'), ('INPUT', '#ABB7C5'),
    ('OUTPUT', '#ABB7C5'), ('SPLIT', '#ABB7C5'), ('MIXER', '#ABB7C5'),
    ('METRONOME', '#ABB7C5'), ('NAM', '#59D9CB'),
)
ALIASES = {
    "1x15 Tuck n' Go": '1x15 Ampeg B-15',
    '2x12 Mandarin': '2x12 Mandarin 30',
    '2x15 US Dripman': '2x15 Dripman',
    '4x12 1960A T75': '4x12 1960 T75',
    '4x12 Blckbck H30': '4x12 Blackback 30',
    '4x12 Mandarin': '4x12 Mandarin EM',
    '8x10 SV Beast': '8x10 Ampeg SVT E',
    'Alpaca Rouge': 'Alpaca Rogue',
    'GrammaticoLG Jmp': 'GrammaticoLG Jump',
    'HX Room': 'Room',
    'Infinite Sustain': 'Poly Sustain',
    'Line 6 Bigger': 'Line 6 Badonk',
    'Low Res': 'Lo Res',
    'Mandarin Bass 200': 'Mandarin 200',
    'Pillars': 'Pillars OD',
    'ShuflMon': 'Glitch Delay',
    'Tremolo': 'Tremolo/Autopan',
    'US Dripman Norm': 'US Dripman Nrm',
    'US Super Norm': 'US Super Nrm',
    'Wringer': 'Wringer Fuzz',
}


def normalized(name):
    return re.sub(r'[^a-z0-9]', '', name.casefold())


def fetch(url):
    request = Request(url, headers={'User-Agent': 'Mozilla/5.0 (HXtra Model Pictures builder)'})
    with urlopen(request, timeout=30) as stream:
        data = stream.read(4 * 1024 * 1024 + 1)
    if len(data) > 4 * 1024 * 1024:
        raise ValueError(f'Artwork response too large: {url}')
    return data


def fetch_artwork(record, cache):
    path = cache / record['asset']
    expected = record['source_sha256']
    if path.is_file() and hashlib.sha256(path.read_bytes()).hexdigest() == expected:
        return
    data = fetch(record['source_url'])
    if hashlib.sha256(data).hexdigest() != expected:
        raise ValueError(f'Artwork SHA-256 changed: {record["source_url"]}')
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)


def tone3000_logo(cache, supplied):
    if supplied is not None:
        return supplied
    path = cache / 'tone3000/tone3000-site-logo.png'
    if path.is_file():
        return path
    page = fetch(TONE3000).decode('utf-8')
    for svg in re.findall(r'<svg\b.*?</svg>', page, flags=re.S):
        if re.search(r'<title>Tone3000[^<]*</title>', svg, flags=re.I):
            break
    else:
        raise ValueError('TONE3000 logo changed location. Supply its PNG with --tone3000-logo.')
    root = ElementTree.fromstring(svg)
    if any(node.tag.split('}')[-1] not in ('svg', 'title', 'g', 'path') or
           any(key.split('}')[-1] in ('href', 'style') for key in node.attrib)
           for node in root.iter()):
        raise ValueError('Unexpected TONE3000 logo markup. Supply its PNG with --tone3000-logo.')
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory() as temp:
        source = Path(temp) / 'tone3000.svg'
        logo = Path(temp) / 'tone3000.png'
        source.write_text(svg)
        subprocess.run(['ffmpeg', '-v', 'error', '-i', str(source),
                        '-frames:v', '1', str(logo)], check=True)
        path.write_bytes(logo.read_bytes())
    return path


def model_art(models, pid_by_index):
    catalog = json.loads(CATALOG.read_text())
    by_name = {}
    for row in catalog['models']:
        by_name.setdefault(normalized(row['name']), []).append(row)

    matched, missing = [], []
    for model in models:
        name, display = model['name'], model['displayName']
        pid = pid_by_index.get(model['words'][2])
        if pid is None or not display:
            continue
        record = {'internal_name': name, 'display_name': display, 'preset_id': pid}
        if name in ('HD2_DistNAMLite', 'HD2_AmpNAMLite'):
            record.update(asset='tone3000/tone3000-site-logo.png', source_url=TONE3000)
            matched.append(record)
            continue

        rows = by_name.get(normalized(ALIASES.get(display, display)), [])
        if name.startswith('HD2_Looper'):
            looper = '1 Switch Looper' if 'OneSwitch' in name else '6 Switch Looper'
            rows = by_name.get(normalized(looper), [])
        if name.startswith(('HD2_AppDSPFlow', 'HelixStomp_AppDSPFlow')):
            rows = []
        if name.startswith('HD2_Cab'):
            rows = [row for row in rows if row['category'] == 'CAB MODELS']
        elif name.startswith(('HD2_Amp', 'HD2_Preamp')):
            rows = [row for row in rows if row['category'] == 'AMP MODELS']
        else:
            rows = [row for row in rows if row['category'] not in ('AMP MODELS', 'CAB MODELS')]

        if not rows:
            missing.append(record)
            continue
        # The Line 6 table lists current models first and Legacy variants last.
        legacy = name.startswith(('HD2_DL4', 'HD2_Cab')) and not name.startswith('HD2_CabMicIr')
        row = rows[-1] if legacy else rows[0]
        record.update(asset=row['source_file'], source_url=row['source_url'], source_sha256=row['sha256'])
        matched.append(record)
    return matched, missing


def branch_target(at, raw):
    a,b=struct.unpack('<HH',raw)
    if a&0xf800!=0xf000 or b&0xd000!=0x9000:
        raise ValueError(f'Expected existing b.w at {at:#x}: {raw.hex()}')
    s=(a>>10)&1;j1=(b>>13)&1;j2=(b>>11)&1
    offset=(s<<24)|((~(j1^s)&1)<<23)|((~(j2^s)&1)<<22)|((a&1023)<<12)|((b&2047)<<1)
    return at+4+offset-((1<<25) if s else 0)


def encode(image):
    image=image.convert('RGBA');out=bytearray()
    for x in range(image.width):
        for y in reversed(range(image.height)):
            r,g,b,a=image.getpixel((x,y))
            out+=struct.pack('<H',image_store.pixel(r*a//255,g*a//255,b*a//255))
    return bytes(out)


def artwork(original, size=(74, 54)):
    im = original.convert('RGBA')
    if im.getchannel('A').getextrema() == (255, 255):
        pixels = np.array(im)
        rgb = pixels[:, :, :3].astype(np.float32)
        distance = 255 - rgb.min(axis=2)
        # Only remove the near-white field connected to the tile's perimeter.
        # Internal highlights and labels can be just as white as the background.
        neutral = rgb.max(axis=2) - rgb.min(axis=2) <= 15
        white = (distance <= 23) & neutral
        perimeter = np.zeros_like(white)
        perimeter[[0, -1], :] = True
        perimeter[:, [0, -1]] = True
        background = ndimage.binary_propagation(perimeter & white, mask=white)
        if background.any() and not background.all():
            # Remove the white contribution of anti-aliased edge pixels before
            # downsampling, otherwise the LCD shows a pale outline on black.
            edge = ndimage.binary_dilation(background, iterations=2) & ~background
            nearby = ndimage.maximum_filter(distance, size=5)
            alpha = np.clip((distance - 4) / np.maximum(nearby - 4, 55), 0, 1)
            alpha = np.where(edge & (distance < 100), alpha, 1)
            alpha[background] = 0
            unmatted = (rgb - (1 - alpha[:, :, None]) * 255) / np.maximum(alpha[:, :, None], 0.001)
            pixels[:, :, :3] = np.clip(unmatted, 0, 255).round().astype(np.uint8)
            pixels[:, :, 3] = (255 * alpha).round().astype(np.uint8)
            im = Image.fromarray(pixels)
    box = im.getbbox()
    if box:
        im = im.crop(box)
    im.thumbnail(size, Image.Resampling.LANCZOS)
    return im


def tinted_title(masks, advances, text, color):
    width = sum(advances[char] for char in text) - 2
    title = Image.new('RGB', (width, 18))
    x = 0
    for char in text:
        if char in masks:
            mask = masks[char]
            title.paste(Image.new('RGB', mask.size, color), (x, 0), mask)
        x += advances[char]
    return title


def title_resources(patch, e7, e8):
    _, table = image_store.font(e7, b'hx21_regularFont')
    entries = bytearray()
    masks, advances = {}, {}
    for code in range(32, 127):
        char = chr(code)
        height, width, item = image_store.glyph(e7, table, code)
        glyph_width = max(1, round(width * 18 / 21))
        glyph_id = 0
        if item != image_store.EMPTY_GLYPH:
            offset, _ = image_store.item(e8, item)
            mask = Image.new('L', (width, height))
            for x in range(width):
                for y in range(height):
                    pixel = struct.unpack_from('<H', e8, offset + 2 * (x * height + height - 1 - y))[0]
                    mask.putpixel((x, y), max(image_store.pixel_rgb(pixel)))
            mask = masks[char] = mask.resize((glyph_width, 18), Image.Resampling.LANCZOS)
            glyph = Image.new('RGB', mask.size)
            glyph.paste(Image.new('RGB', mask.size, '#E8E8E8'), (0, 0), mask)
            glyph_id = patch.add_image(0x43, glyph_width, 18, encode(glyph))
        advances[char] = 5 if char == ' ' else glyph_width + 2
        entries.extend(struct.pack('<HH', glyph_id, advances[char]))
    categories = bytearray()
    for label, color in CATEGORY_STYLES:
        styles = [0, 0, 0, 0]
        if label:
            for i, text in enumerate((label, label + ':')):
                title = tinted_title(masks, advances, text, color)
                styles[i * 2:i * 2 + 2] = (
                    patch.add_image(0x43, title.width, title.height, encode(title)),
                    title.width,
                )
        categories.extend(struct.pack('<4H10s', *styles, label.encode('ascii')))
    return patch.add(bytes(entries)), patch.add(bytes(categories))


def compile_module(work,defines):
    source=ROOT/'src/m4/model_pictures.c';assembly=work/'model_pictures.generated.s'
    subprocess.run([m4asm.CLANG,'-target','thumbv7em-none-eabi','-mcpu=cortex-m4','-mfpu=fpv4-sp-d16','-mfloat-abi=hard',
                    '-ffreestanding','-fno-builtin','-fPIC','-fno-unwind-tables','-fno-asynchronous-unwind-tables','-Oz',
                    '-Wall','-Wextra','-Werror',*[f'-D{k}={v}' for k,v in defines.items()],'-S',str(source),'-o',str(assembly)],check=True)
    text=assembly.read_text()
    # A single self-contained code/data section makes all local references
    # position independent. m4asm rejects any remaining relocation.
    text=re.sub(r'^\s*\.globl[^\n]*','',text,flags=re.M)
    text=re.sub(r'^\s*\.(?:fnstart|fnend|cantunwind|save|pad|setfp)[^\n]*','',text,flags=re.M)
    text=re.sub(r'^\s*\.section\s+\.(?:rodata|data|bss)[^\n]*','\n.text',text,flags=re.M)
    text=re.sub(r'(\.p2align\s+\d+),\s*(?:0x0+|0+)(?=\s|$)',r'\1',text)
    assembly.write_text(text)
    return m4asm.assemble_module(str(assembly))


def build(base, out, work, cache, logo_path):
    data,_=pa.load_l6ia_from_any(str(base))
    load=lambda i:pa.get_entry_swapped(data,i)
    patch=pm.Patch(load(0),[],{},load=load);patch.owner='model_pictures'
    e7,e8=load(7),load(8)
    original_count=struct.unpack_from('<I',e7,0x18)[0]
    models=pa.parse_entry3_full(load(3))['models']
    pid_by_index={}
    table=patch.u32(0x28e01c08)
    _,n=pa._decode_thumb_movw(patch.read(0x28e01c74,4))
    for i in range(n):
        idx,pid,_,_=struct.unpack('<IHHI',patch.read(table+12*i,12));pid_by_index[idx]=pid
    matched, missing = model_art(models, pid_by_index)
    artwork_by_path = {r['asset']:r for r in matched if 'source_sha256' in r}
    print(f'Checking {len(artwork_by_path)} catalog images in {cache}', flush=True)
    with ThreadPoolExecutor(max_workers=6) as pool:
        list(pool.map(lambda record: fetch_artwork(record, cache), artwork_by_path.values()))
    logo = tone3000_logo(cache, logo_path)
    pid_images = [0] * (max(pid_by_index.values()) + 1)
    picture_layout = [(0, 0, 0)] * len(pid_images)
    nam_pid = next(record['preset_id'] for record in matched
                   if record['internal_name'] == 'HD2_DistNAMLite')
    nam_edit_image = 0
    icons = {}
    for record in matched:
        key = record.get('source_sha256', record['asset'])
        if key not in icons:
            source = cache / record['asset'] if 'source_sha256' in record else logo
            with Image.open(source) as original:
                im = artwork(original)
                icon = patch.add_image(0x43, im.width, im.height, encode(im))
                compact_id, compact_height = icon, im.height
                if im.height > 37:
                    compact = im.copy()
                    compact.thumbnail((74, 37), Image.Resampling.LANCZOS)
                    compact_id = patch.add_image(0x43, compact.width, compact.height, encode(compact))
                    compact_height = compact.height
                icons[key] = icon, (compact_id, im.height, compact_height)
        pid = record['preset_id']
        icon, layout = icons[key]
        if pid_images[pid] not in (0, icon):
            raise ValueError(f'Ambiguous artwork for preset ID {pid}')
        pid_images[pid] = icon
        picture_layout[pid] = layout
        record['image_id'] = icon
        if pid == nam_pid:
            with Image.open(logo) as original:
                logo = artwork(original, (128, 20))
            nam_edit_image = patch.add_image(0x43, logo.width, logo.height, encode(logo))
            picture_layout[pid] = (icon, logo.height, layout[1])
            record['edit_image_id'] = nam_edit_image
    index_to_pid=[0xffff]*(max(pid_by_index,default=0)+1)
    for model_index,pid in pid_by_index.items():
        if model_index<len(index_to_pid):index_to_pid[model_index]=pid
    index_pid_at=patch.add(struct.pack(f'<{len(index_to_pid)}H',*index_to_pid))
    # Preserve graph geometry with vertically compact copies of its images.
    small=[0]*original_count
    # Preserve every original signal-chain icon pixel for pixel.
    for i in range(original_count):small[i]=i
    pid_at=patch.add(struct.pack(f'<{len(pid_images)}I',*pid_images))
    picture_layout_at = patch.add(b''.join(struct.pack('<HBB', *item) for item in picture_layout))
    model_names = bytearray(b'\0')
    name_offsets = [0] * len(pid_images)
    prefer_hxtra_name = bytearray(len(pid_images))
    category_overrides = bytearray(len(pid_images))
    offsets_by_name = {}
    input_pid = next(record['preset_id'] for record in missing
                     if record['internal_name'] == 'HelixStomp_AppDSPFlowInput')
    for record in matched + missing:
        pid = record['preset_id']
        internal = record['internal_name']
        display_name = record['display_name']
        name = 'Input' if pid == input_pid else real_names.real_name(internal, display_name)
        # Values above the browser's final category (Looper) are local titles.
        if pid == input_pid:
            category_overrides[pid] = 19  # Input
        elif internal.startswith('HelixStomp_AppDSPFlowOutput'):
            category_overrides[pid] = 20  # Output
        elif internal.startswith('HD2_AppDSPFlowSplit'):
            category_overrides[pid] = 21  # Split
        elif internal == 'HD2_AppDSPFlowJoin':
            category_overrides[pid] = 22  # Mixer
        elif internal == 'HD2_ClickMetronomeMono':
            category_overrides[pid] = 23  # Metronome
        elif pid == nam_pid:
            category_overrides[pid] = 24  # NAM
        if name not in offsets_by_name:
            offsets_by_name[name] = len(model_names)
            model_names.extend(name.encode('ascii', 'replace') + b'\0')
        name_offsets[pid] = offsets_by_name[name]
        prefer_hxtra_name[pid] = pid != input_pid and name != display_name
    names_at = patch.add(bytes(model_names))
    offsets_at = patch.add(struct.pack(f'<{len(name_offsets)}H', *name_offsets))
    prefer_hxtra_name_at = patch.add(bytes(prefer_hxtra_name))
    categories_at = patch.add(bytes(category_overrides))
    glyphs_at, styles_at = title_resources(patch, e7, e8)
    small_at=patch.add(struct.pack(f'<{len(small)}I',*small))
    state_at=patch.add(bytes(256))
    resource_names = image_store.names(e7)
    stereo_image = resource_names.index(b'Icon_Stereo')
    stereo_off_image = resource_names.index(b'Icon_StereoOff')
    native_stereo = image_store.image_at(e7, e8, stereo_image)
    stereo_pixels = e8[native_stereo.offset:native_stereo.offset + native_stereo.length]
    stereo_pixels, native_width, native_height = image_store.trimmed(
        stereo_pixels, native_stereo.width, native_stereo.height)
    stereo_width, stereo_height = 21, 14
    badge = patch.add_image(stereo_image, stereo_width, stereo_height, image_store.resized(
        stereo_pixels, native_width, native_height, stereo_width, stereo_height))
    count=original_count+len(patch.entry(7).records)
    setting, = model_pictures.SETTINGS
    defines={'STATE_ADDRESS':hex(state_at),'PID_TABLE':hex(pid_at),'PID_COUNT':len(pid_images),
             'GET_SETTING':hex(pm.GET_VALUE),
             'MODEL_PICTURES_SETTING':setting_ids.by_name()[f'model_pictures.{setting.key}'],
             'MODEL_PICTURES_DEFAULT':setting.default,'MODEL_PICTURES_MAX':setting.max,
             'MODEL_PICTURES_OFF':setting.values.index(b'Off'),
             'STEREO_IMAGE':stereo_image,'STEREO_OFF_IMAGE':stereo_off_image,
             'STEREO_BADGE':badge,'STEREO_WIDTH':stereo_width,
             'PICTURE_LAYOUT':hex(picture_layout_at),
             'MODEL_NAMES':hex(names_at),'NAME_OFFSETS':hex(offsets_at),'TITLE_GLYPHS':hex(glyphs_at),
             'PREFER_HXTRA_NAME':hex(prefer_hxtra_name_at),
             'CATEGORY_OVERRIDES':hex(categories_at),
             'CATEGORY_STYLES':hex(styles_at),'CATEGORY_COUNT':len(CATEGORY_STYLES),
             'INPUT_PID':input_pid,'NAM_PID':nam_pid,'NAM_EDIT_IMAGE':nam_edit_image,
             'SMALL_TABLE':hex(small_at),'SMALL_COUNT':len(small),'IMAGE_COUNT':count,'INDEX_PID_TABLE':hex(index_pid_at),'MODEL_INDEX_COUNT':len(index_to_pid)}
    code,symbols=compile_module(work,defines)
    at=patch.add(code)
    if patch.next()>=0x29000000:raise ValueError('Application crosses the RAM overlay boundary')
    functions={name:at+symbols[name] for name in ['mp_edit','mp_browser']}
    # Keep every previous paint callback, including screen meter wrappers.
    for literal,after in [(0x28d96da0,'mp_edit'),(0x28d96e38,'mp_edit'),(0x28d96df0,'mp_browser')]:
        previous=patch.u32(literal)
        patch.chain_ptr(literal,previous,after=functions[after])
    patch.finish_chains();entries=patch.output()
    assert set(entries)=={0,7,8},set(entries)
    image_store.check(entries[7],entries[8])
    if len(entries[8]) > IMAGE_LIMIT:
        raise ValueError(f'Image store exceeds its boot read window: {len(entries[8])} bytes')
    result,_,_=l6ia.rebuild_archive(data,entries)
    pa.save_l6ia_to_any(result,str(out),donor_hxf=str(base))
    for i in range(15):
        if i not in entries:assert pa.get_entry_swapped(result,i)==load(i),i
    report={'source':str(base),'output':str(out),'sha256':hashlib.sha256(out.read_bytes()).hexdigest(),
            'changed_entries':sorted(entries),'preserved_entries':[i for i in range(15) if i not in entries],
            'model_illustrations':len(icons),'mapped_preset_ids':sum(bool(x) for x in pid_images),
            'model_mapping':matched,'unmatched_models':missing,
            'native_graph_images':sum(bool(x) for x in small),'entry8_bytes':len(entries[8]),
            'module_at':at,'module_bytes':len(code),'symbols':symbols,'defines':defines,'functions':functions,
            'module_sha256':hashlib.sha256(code).hexdigest(),'hardware_tested':False,
            'note':'Line 6 model illustrations and the TONE3000 logo for NAM A2 Lite. Unmatched models retain their native block icons. Default Edit view. Offline build.'}
    (work/'model_pictures.bin').write_bytes(code)
    out.with_suffix('.hxf.json').write_text(json.dumps(report,indent=2))
    (work/'build.json').write_text(json.dumps(report,indent=2))
    print(f'Model Pictures: wrote {out} (SHA-256 {report["sha256"]})')


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('stock', type=Path, help='your copy of the original HX Stomp 3.80.0 HXF')
    parser.add_argument('-o', '--out', type=Path, default=BUILD_ROOT/'HX_Stomp_3.80.0_Model_Pictures.hxf')
    parser.add_argument('--art-cache', type=Path, default=BUILD_ROOT/'art')
    parser.add_argument('--tone3000-logo', type=Path, help='local TONE3000 logo PNG')
    parser.add_argument('--splash', type=Path, help='optional boot splash PNG accepted by build.py')
    parser.add_argument('--no-check', action='store_true', help='skip the upstream NAM simulator test')
    args=parser.parse_args()
    stock, out = args.stock.resolve(), args.out.resolve()
    if out==stock or out.suffix.lower()!='.hxf':
        parser.error('choose a separate .hxf output file')
    out.parent.mkdir(parents=True,exist_ok=True)
    BUILD_ROOT.mkdir(parents=True,exist_ok=True)
    env={**os.environ, **{'HX_'+name:'1' for name in FEATURE_FLAGS}, 'HX_NAM_SLOTS':'2'}
    with tempfile.TemporaryDirectory(prefix='build-',dir=BUILD_ROOT) as tmp:
        work=Path(tmp)
        base=work/'hxtra.hxf'
        cmd=[sys.executable,str(ROOT/'build.py'),str(stock),'-o',str(base)]
        if args.splash:
            cmd+=['--splash',str(args.splash.resolve())]
        if args.no_check:
            cmd.append('--no-check')
        subprocess.run(cmd,cwd=ROOT,env=env,check=True)
        build(base,out,work,args.art_cache.resolve(),args.tone3000_logo)


if __name__=='__main__':
    main()
