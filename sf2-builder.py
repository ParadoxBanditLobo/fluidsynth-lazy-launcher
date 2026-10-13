#!/usr/bin/env python3
import argparse
import json
import struct
import sys
from pathlib import Path

PRESET_GEN_INSTRUMENT = 41
INST_GEN_SAMPLE_ID = 53

def u16(data, off):
    return struct.unpack_from('<H', data, off)[0]

def u32(data, off):
    return struct.unpack_from('<I', data, off)[0]

def p16(v):
    return struct.pack('<H', v)

def p32(v):
    return struct.pack('<I', v)

def pad2(data):
    return data + (b'\x00' if len(data) & 1 else b'')

def chunk(tag, payload):
    return tag + p32(len(payload)) + pad2(payload)

def list_chunk(kind, payload):
    body = kind + payload
    return b'LIST' + p32(len(body)) + pad2(body)

def riff_sfbk(payload):
    body = b'sfbk' + payload
    return b'RIFF' + p32(len(body)) + body

def read_chunks(data, start, end):
    pos = start
    while pos + 8 <= end:
        tag = data[pos:pos+4]
        size = u32(data, pos + 4)
        body = pos + 8
        stop = body + size
        if stop > end:
            raise ValueError('Invalid RIFF chunk length')
        yield tag, data[body:stop]
        pos = stop + (size & 1)

def fixed_name(raw):
    return raw.split(b'\x00', 1)[0].decode('latin1', 'replace')

def name20(text):
    raw = text.encode('latin1', 'replace')[:20]
    return raw + b'\x00' * (20 - len(raw))

class SF2:
    def __init__(self, path):
        self.path = Path(path)
        if self.path.suffix.lower() != '.sf2':
            raise ValueError(f'{self.path.name}: builder currently supports .sf2 sources only')
        self.data = self.path.read_bytes()
        if len(self.data) < 12 or self.data[:4] != b'RIFF' or self.data[8:12] != b'sfbk':
            raise ValueError(f'{self.path.name}: not a valid SoundFont 2 file')
        riff_size = u32(self.data, 4)
        riff_end = min(len(self.data), 8 + riff_size)
        self.info_chunks = []
        self.smpl = None
        self.pdta = {}
        for tag, body in read_chunks(self.data, 12, riff_end):
            if tag != b'LIST' or len(body) < 4:
                continue
            kind = body[:4]
            sub = body[4:]
            if kind == b'INFO':
                self.info_chunks = list(read_chunks(sub, 0, len(sub)))
            elif kind == b'sdta':
                for stag, sbody in read_chunks(sub, 0, len(sub)):
                    if stag == b'smpl':
                        self.smpl = sbody
                    elif stag == b'sm24':
                        raise ValueError(f'{self.path.name}: 24-bit sm24 SoundFonts are not supported by the builder yet')
            elif kind == b'pdta':
                self.pdta = {stag: sbody for stag, sbody in read_chunks(sub, 0, len(sub))}
        if self.smpl is None:
            raise ValueError(f'{self.path.name}: no uncompressed sample data found')
        needed = [b'phdr', b'pbag', b'pmod', b'pgen', b'inst', b'ibag', b'imod', b'igen', b'shdr']
        missing = [x.decode() for x in needed if x not in self.pdta]
        if missing:
            raise ValueError(f'{self.path.name}: missing pdta chunks: {", ".join(missing)}')
        self.phdr = self._records(b'phdr', 38)
        self.pbag = self._records(b'pbag', 4)
        self.pmod = self._records(b'pmod', 10)
        self.pgen = self._records(b'pgen', 4)
        self.inst = self._records(b'inst', 22)
        self.ibag = self._records(b'ibag', 4)
        self.imod = self._records(b'imod', 10)
        self.igen = self._records(b'igen', 4)
        self.shdr = self._records(b'shdr', 46)
        if len(self.phdr) < 1 or len(self.inst) < 1 or len(self.shdr) < 1:
            raise ValueError(f'{self.path.name}: malformed terminal records')

    def _records(self, tag, size):
        body = self.pdta[tag]
        if len(body) % size:
            raise ValueError(f'{self.path.name}: malformed {tag.decode()} table')
        return [body[i:i+size] for i in range(0, len(body), size)]

    def presets(self):
        out = []
        for i, rec in enumerate(self.phdr[:-1]):
            out.append({
                'index': i,
                'name': fixed_name(rec[:20]),
                'program': u16(rec, 20),
                'bank': u16(rec, 22),
            })
        return out

    def preset_zone_range(self, preset_index):
        a = u16(self.phdr[preset_index], 24)
        b = u16(self.phdr[preset_index + 1], 24)
        return a, b

    def inst_zone_range(self, inst_index):
        a = u16(self.inst[inst_index], 20)
        b = u16(self.inst[inst_index + 1], 20)
        return a, b

    def bag_gen_range(self, table, bag_index):
        a = u16(table[bag_index], 0)
        b = u16(table[bag_index + 1], 0)
        return a, b

    def bag_mod_range(self, table, bag_index):
        a = u16(table[bag_index], 2)
        b = u16(table[bag_index + 1], 2)
        return a, b

    def preset_instruments(self, preset_index):
        found = set()
        a, b = self.preset_zone_range(preset_index)
        for bag in range(a, b):
            ga, gb = self.bag_gen_range(self.pbag, bag)
            for rec in self.pgen[ga:gb]:
                if u16(rec, 0) == PRESET_GEN_INSTRUMENT:
                    found.add(u16(rec, 2))
        return found

    def instrument_samples(self, inst_index):
        found = set()
        a, b = self.inst_zone_range(inst_index)
        for bag in range(a, b):
            ga, gb = self.bag_gen_range(self.ibag, bag)
            for rec in self.igen[ga:gb]:
                if u16(rec, 0) == INST_GEN_SAMPLE_ID:
                    found.add(u16(rec, 2))
        return found

def patch_gen(rec, new_amount):
    return rec[:2] + p16(new_amount)

def clone_mods(records, start, end):
    return records[start:end]

def build_soundfont(selections, output, bank_name='FSL Custom Bank'):
    sources = {}
    for sel in selections:
        src = sel['source']
        sources.setdefault(src, SF2(src))

    out_phdr = []
    out_pbag = []
    out_pmod = []
    out_pgen = []
    out_inst = []
    out_ibag = []
    out_imod = []
    out_igen = []
    out_shdr = []
    out_smpl = bytearray()

    inst_map = {}
    sample_map = {}

    def copy_sample(sf, sample_index):
        key = (str(sf.path), sample_index)
        if key in sample_map:
            return sample_map[key]
        if not (0 <= sample_index < len(sf.shdr) - 1):
            raise ValueError(f'{sf.path.name}: invalid sample index {sample_index}')
        rec = sf.shdr[sample_index]
        start = u32(rec, 20)
        end = u32(rec, 24)
        loop_start = u32(rec, 28)
        loop_end = u32(rec, 32)
        rate = u32(rec, 36)
        orig_pitch = rec[40]
        pitch_corr = struct.unpack_from('<b', rec, 41)[0]
        link = u16(rec, 42)
        sample_type = u16(rec, 44)
        if end < start or end * 2 > len(sf.smpl):
            raise ValueError(f'{sf.path.name}: invalid sample bounds for {fixed_name(rec[:20])}')

        new_index = len(out_shdr)
        sample_map[key] = new_index

        new_start = len(out_smpl) // 2
        pcm = sf.smpl[start*2:end*2]
        out_smpl.extend(pcm)
        new_end = len(out_smpl) // 2
        out_smpl.extend(b'\x00' * (46 * 2))

        rel_loop_start = max(0, min(end - start, loop_start - start))
        rel_loop_end = max(rel_loop_start, min(end - start, loop_end - start))

        out_shdr.append({
            'name': fixed_name(rec[:20]),
            'start': new_start,
            'end': new_end,
            'loop_start': new_start + rel_loop_start,
            'loop_end': new_start + rel_loop_end,
            'rate': rate,
            'orig_pitch': orig_pitch,
            'pitch_corr': pitch_corr,
            'old_link': link,
            'sample_type': sample_type,
            'source': sf,
            'source_index': sample_index,
        })

        base_type = sample_type & 0x7fff
        if base_type in (2, 4) and 0 <= link < len(sf.shdr) - 1:
            copy_sample(sf, link)
        return new_index

    def copy_instrument(sf, inst_index):
        key = (str(sf.path), inst_index)
        if key in inst_map:
            return inst_map[key]
        if not (0 <= inst_index < len(sf.inst) - 1):
            raise ValueError(f'{sf.path.name}: invalid instrument index {inst_index}')

        new_index = len(out_inst)
        inst_map[key] = new_index
        rec = sf.inst[inst_index]
        out_inst.append({'name': fixed_name(rec[:20]), 'ibag': len(out_ibag)})

        a, b = sf.inst_zone_range(inst_index)
        for bag in range(a, b):
            ga, gb = sf.bag_gen_range(sf.ibag, bag)
            ma, mb = sf.bag_mod_range(sf.ibag, bag)
            out_ibag.append((len(out_igen), len(out_imod)))
            for grec in sf.igen[ga:gb]:
                if u16(grec, 0) == INST_GEN_SAMPLE_ID:
                    sid = u16(grec, 2)
                    new_sid = copy_sample(sf, sid)
                    out_igen.append(patch_gen(grec, new_sid))
                else:
                    out_igen.append(grec)
            out_imod.extend(clone_mods(sf.imod, ma, mb))
        return new_index

    used_slots = set()
    for sel in selections:
        sf = sources[sel['source']]
        pi = int(sel['preset_index'])
        if not (0 <= pi < len(sf.phdr) - 1):
            raise ValueError(f'{sf.path.name}: invalid preset index {pi}')
        bank = int(sel.get('bank', 0))
        program = int(sel.get('program', 0))
        if not (0 <= bank <= 16383 and 0 <= program <= 127):
            raise ValueError('Output bank/program is out of range')
        key = (bank, program)
        if key in used_slots:
            raise ValueError(f'Duplicate output bank/program: bank {bank}, program {program}')
        used_slots.add(key)

        name = sel.get('name') or fixed_name(sf.phdr[pi][:20])
        out_phdr.append({
            'name': name,
            'program': program,
            'bank': bank,
            'pbag': len(out_pbag),
        })

        a, b = sf.preset_zone_range(pi)
        for bag in range(a, b):
            ga, gb = sf.bag_gen_range(sf.pbag, bag)
            ma, mb = sf.bag_mod_range(sf.pbag, bag)
            out_pbag.append((len(out_pgen), len(out_pmod)))
            for grec in sf.pgen[ga:gb]:
                if u16(grec, 0) == PRESET_GEN_INSTRUMENT:
                    ii = u16(grec, 2)
                    new_ii = copy_instrument(sf, ii)
                    out_pgen.append(patch_gen(grec, new_ii))
                else:
                    out_pgen.append(grec)
            out_pmod.extend(clone_mods(sf.pmod, ma, mb))

    # Terminal bag records.
    out_pbag.append((len(out_pgen), len(out_pmod)))
    out_ibag.append((len(out_igen), len(out_imod)))

    # Patch linked sample indexes now that all samples are known.
    shdr_bytes = bytearray()
    for item in out_shdr:
        link = 0
        if (item['sample_type'] & 0x7fff) in (2, 4):
            link = sample_map.get((str(item['source'].path), item['old_link']), 0)
        shdr_bytes.extend(
            name20(item['name']) +
            p32(item['start']) + p32(item['end']) +
            p32(item['loop_start']) + p32(item['loop_end']) +
            p32(item['rate']) +
            bytes([item['orig_pitch']]) +
            struct.pack('<b', item['pitch_corr']) +
            p16(link) + p16(item['sample_type'])
        )
    eos_pos = len(out_smpl) // 2
    shdr_bytes.extend(name20('EOS') + p32(eos_pos) * 4 + p32(44100) + b'\x00\x00' + p16(0) + p16(1))

    phdr_bytes = bytearray()
    for item in out_phdr:
        phdr_bytes.extend(
            name20(item['name']) +
            p16(item['program']) + p16(item['bank']) + p16(item['pbag']) +
            p32(0) + p32(0) + p32(0)
        )
    phdr_bytes.extend(name20('EOP') + p16(0) + p16(0) + p16(len(out_pbag) - 1) + p32(0) * 3)

    inst_bytes = bytearray()
    for item in out_inst:
        inst_bytes.extend(name20(item['name']) + p16(item['ibag']))
    inst_bytes.extend(name20('EOI') + p16(len(out_ibag) - 1))

    pbag_bytes = b''.join(p16(g) + p16(m) for g, m in out_pbag)
    ibag_bytes = b''.join(p16(g) + p16(m) for g, m in out_ibag)

    info = (
        chunk(b'ifil', p16(2) + p16(1)) +
        chunk(b'isng', b'EMU8000\x00') +
        chunk(b'INAM', bank_name.encode('latin1', 'replace')[:255] + b'\x00') +
        chunk(b'ICRD', b'Built with FluidSynth Lazy Launcher\x00') +
        chunk(b'ICMT', b'Use responsibly: source sounds keep their original licenses.\x00')
    )
    sdta = chunk(b'smpl', bytes(out_smpl))
    pdta = (
        chunk(b'phdr', bytes(phdr_bytes)) +
        chunk(b'pbag', pbag_bytes) +
        chunk(b'pmod', b''.join(out_pmod)) +
        chunk(b'pgen', b''.join(out_pgen)) +
        chunk(b'inst', bytes(inst_bytes)) +
        chunk(b'ibag', ibag_bytes) +
        chunk(b'imod', b''.join(out_imod)) +
        chunk(b'igen', b''.join(out_igen)) +
        chunk(b'shdr', bytes(shdr_bytes))
    )
    result = riff_sfbk(list_chunk(b'INFO', info) + list_chunk(b'sdta', sdta) + list_chunk(b'pdta', pdta))
    output = Path(output)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_bytes(result)

def interactive(slots, output_dir, result_file=''):
    loaded = {}
    for slot, path in slots.items():
        if path and Path(path).is_file():
            if Path(path).suffix.lower() == '.sf2':
                loaded[slot] = SF2(path)

    print('SoundFont Builder')
    print('=================')
    print('Use responsibly: only combine sounds you have permission to use.')
    print()
    if not loaded:
        print('No .sf2 SoundFonts are loaded in slots A/B/C.')
        return 1

    selections = []
    while True:
        print('\nLoaded sources:')
        for slot in ('A','B','C'):
            if slot in loaded:
                print(f'  {slot}: {loaded[slot].path.name}')
        print('\nSelected presets:')
        if not selections:
            print('  None')
        else:
            for i, s in enumerate(selections, 1):
                print(f"  {i}. slot {s['slot']} / {s['source_name']} -> bank {s['bank']}, program {s['program']}: {s['name']}")

        print('\n1. Add preset')
        print('2. Remove selected preset')
        print('3. Build custom SF2')
        print('4. Cancel')
        choice = input('\nChoose an option: ').strip()

        if choice == '1':
            slot = input('Source slot (A/B/C): ').strip().upper()
            if slot not in loaded:
                print('That slot does not contain a supported .sf2 file.')
                continue
            sf = loaded[slot]
            presets = sf.presets()
            print(f'\nPresets in {slot}:')
            for i, p in enumerate(presets, 1):
                print(f"{i:3d}. bank {p['bank']:3d}  program {p['program']:3d}  {p['name']}")
            try:
                pick = int(input('Preset number (0 cancels): ').strip())
            except ValueError:
                print('Invalid preset.')
                continue
            if pick == 0:
                continue
            if not 1 <= pick <= len(presets):
                print('Invalid preset.')
                continue
            p = presets[pick - 1]
            default_program = len(selections) % 128
            default_bank = len(selections) // 128
            rename = input(f"Output name [{p['name']}]: ").strip() or p['name']
            raw_bank = input(f'Output bank [{default_bank}]: ').strip()
            raw_prog = input(f'Output program 0-127 [{default_program}]: ').strip()
            try:
                bank = int(raw_bank) if raw_bank else default_bank
                program = int(raw_prog) if raw_prog else default_program
            except ValueError:
                print('Invalid bank/program.')
                continue
            selections.append({
                'slot': slot,
                'source': str(sf.path),
                'source_name': sf.path.name,
                'preset_index': p['index'],
                'name': rename,
                'bank': bank,
                'program': program,
            })

        elif choice == '2':
            if not selections:
                continue
            try:
                pick = int(input('Selected preset number to remove: ').strip())
            except ValueError:
                continue
            if 1 <= pick <= len(selections):
                selections.pop(pick - 1)

        elif choice == '3':
            if not selections:
                print('Select at least one preset first.')
                continue
            bank_name = input('Custom SoundFont name [FSL Custom Bank]: ').strip() or 'FSL Custom Bank'
            filename = input('Output filename [custom-bank.sf2]: ').strip() or 'custom-bank.sf2'
            if not filename.lower().endswith('.sf2'):
                filename += '.sf2'
            out = Path(output_dir) / filename
            if out.exists():
                base = out.stem
                suffix = out.suffix
                n = 2
                while out.exists():
                    out = out.with_name(f'{base} ({n}){suffix}')
                    n += 1
            build_soundfont(selections, out, bank_name)
            if result_file:
                Path(result_file).write_text(str(out), encoding='utf-8')
            print(f'Built SoundFont: {out}')
            return 0

        elif choice == '4':
            return 1
        else:
            print('Invalid option.')

def main():
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest='cmd', required=True)

    p = sub.add_parser('list')
    p.add_argument('sf2')

    p = sub.add_parser('build')
    p.add_argument('spec_json')
    p.add_argument('output')

    p = sub.add_parser('interactive')
    p.add_argument('output_dir')
    p.add_argument('--slot-a', default='')
    p.add_argument('--slot-b', default='')
    p.add_argument('--slot-c', default='')
    p.add_argument('--result-file', default='')

    args = ap.parse_args()
    try:
        if args.cmd == 'list':
            sf = SF2(args.sf2)
            for p in sf.presets():
                print(f"{p['index']}|{p['bank']}|{p['program']}|{p['name']}")
        elif args.cmd == 'build':
            spec = json.loads(Path(args.spec_json).read_text(encoding='utf-8'))
            build_soundfont(spec['selections'], args.output, spec.get('name', 'FSL Custom Bank'))
        else:
            raise SystemExit(interactive(
                {'A': args.slot_a, 'B': args.slot_b, 'C': args.slot_c},
                args.output_dir,
                args.result_file,
            ))
    except Exception as e:
        print(f'Error: {e}', file=sys.stderr)
        sys.exit(1)

if __name__ == '__main__':
    main()
