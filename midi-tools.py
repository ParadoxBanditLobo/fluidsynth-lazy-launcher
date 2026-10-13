#!/usr/bin/env python3
import argparse, json, struct, sys, subprocess, time, shutil
from pathlib import Path

GM_NAMES = [
'Acoustic Grand Piano','Bright Acoustic Piano','Electric Grand Piano','Honky-tonk Piano','Electric Piano 1','Electric Piano 2','Harpsichord','Clavinet',
'Celesta','Glockenspiel','Music Box','Vibraphone','Marimba','Xylophone','Tubular Bells','Dulcimer',
'Drawbar Organ','Percussive Organ','Rock Organ','Church Organ','Reed Organ','Accordion','Harmonica','Tango Accordion',
'Acoustic Guitar (nylon)','Acoustic Guitar (steel)','Electric Guitar (jazz)','Electric Guitar (clean)','Electric Guitar (muted)','Overdriven Guitar','Distortion Guitar','Guitar Harmonics',
'Acoustic Bass','Electric Bass (finger)','Electric Bass (pick)','Fretless Bass','Slap Bass 1','Slap Bass 2','Synth Bass 1','Synth Bass 2',
'Violin','Viola','Cello','Contrabass','Tremolo Strings','Pizzicato Strings','Orchestral Harp','Timpani',
'String Ensemble 1','String Ensemble 2','SynthStrings 1','SynthStrings 2','Choir Aahs','Voice Oohs','Synth Voice','Orchestra Hit',
'Trumpet','Trombone','Tuba','Muted Trumpet','French Horn','Brass Section','SynthBrass 1','SynthBrass 2',
'Soprano Sax','Alto Sax','Tenor Sax','Baritone Sax','Oboe','English Horn','Bassoon','Clarinet',
'Piccolo','Flute','Recorder','Pan Flute','Blown Bottle','Shakuhachi','Whistle','Ocarina',
'Lead 1 (square)','Lead 2 (sawtooth)','Lead 3 (calliope)','Lead 4 (chiff)','Lead 5 (charang)','Lead 6 (voice)','Lead 7 (fifths)','Lead 8 (bass + lead)',
'Pad 1 (new age)','Pad 2 (warm)','Pad 3 (polysynth)','Pad 4 (choir)','Pad 5 (bowed)','Pad 6 (metallic)','Pad 7 (halo)','Pad 8 (sweep)',
'FX 1 (rain)','FX 2 (soundtrack)','FX 3 (crystal)','FX 4 (atmosphere)','FX 5 (brightness)','FX 6 (goblins)','FX 7 (echoes)','FX 8 (sci-fi)',
'Sitar','Banjo','Shamisen','Koto','Kalimba','Bag Pipe','Fiddle','Shanai',
'Tinkle Bell','Agogo','Steel Drums','Woodblock','Taiko Drum','Melodic Tom','Synth Drum','Reverse Cymbal',
'Guitar Fret Noise','Breath Noise','Seashore','Bird Tweet','Telephone Ring','Helicopter','Applause','Gunshot'
]

def read_u16(b, o): return struct.unpack_from('>H', b, o)[0]
def read_u32(b, o): return struct.unpack_from('>I', b, o)[0]

def read_vlq(data, pos):
    value = 0
    start = pos
    for _ in range(4):
        if pos >= len(data): raise ValueError('Unexpected end of track while reading variable-length value')
        c = data[pos]; pos += 1
        value = (value << 7) | (c & 0x7f)
        if not (c & 0x80): return value, pos, data[start:pos]
    raise ValueError('Invalid variable-length value')

def parse_header(data):
    if len(data) < 14 or data[:4] != b'MThd': raise ValueError('Not a Standard MIDI file')
    hlen = read_u32(data, 4)
    if hlen < 6 or len(data) < 8 + hlen: raise ValueError('Invalid MIDI header')
    return read_u16(data, 8), read_u16(data, 10), read_u16(data, 12), 8 + hlen

def iter_tracks(data, start, expected):
    pos = start
    for count in range(expected):
        if pos + 8 > len(data) or data[pos:pos+4] != b'MTrk': raise ValueError(f'Expected MTrk at byte {pos}')
        size = read_u32(data, pos+4)
        body_start = pos + 8
        body_end = body_start + size
        if body_end > len(data): raise ValueError('Track extends beyond end of file')
        yield count, pos, body_start, body_end, data[body_start:body_end]
        pos = body_end

def parse_track(track, division):
    pos = 0; tick = 0; running = None; note_ons = 0
    note_min = 127; note_max = 0; channels = set(); programs = []; tempos = []; name = ''
    while pos < len(track):
        delta, pos, _ = read_vlq(track, pos); tick += delta
        if pos >= len(track): break
        status = track[pos]
        if status < 0x80:
            if running is None: raise ValueError('Running status encountered before status byte')
            status_from_stream = False; status = running
        else:
            pos += 1; status_from_stream = True
            if 0x80 <= status <= 0xEF: running = status
            else: running = None
        if status == 0xFF:
            if pos >= len(track): raise ValueError('Truncated meta event')
            meta_type = track[pos]; pos += 1
            length, pos, _ = read_vlq(track, pos)
            end = pos + length
            if end > len(track): raise ValueError('Truncated meta event')
            payload = track[pos:end]; pos = end
            if meta_type == 0x03: name = payload.decode('latin1', 'replace').strip() or name
            elif meta_type == 0x51 and length == 3:
                us = int.from_bytes(payload, 'big')
                tempos.append((tick, us, 60000000.0 / us if us else 0))
        elif status in (0xF0, 0xF7):
            length, pos, _ = read_vlq(track, pos); pos += length
            if pos > len(track): raise ValueError('Truncated SysEx event')
        elif 0x80 <= status <= 0xEF:
            ch = status & 0x0F; kind = status & 0xF0; channels.add(ch)
            need = 1 if kind in (0xC0, 0xD0) else 2
            if pos + need > len(track): raise ValueError('Truncated channel event')
            d1 = track[pos]; d2 = track[pos+1] if need == 2 else None; pos += need
            if kind == 0x90 and d2 and d2 > 0:
                note_ons += 1; note_min = min(note_min, d1); note_max = max(note_max, d1)
            elif kind == 0xC0: programs.append((tick, ch, d1))
        else: raise ValueError(f'Unsupported status byte 0x{status:02X}')
    if note_ons == 0: note_min = note_max = None
    return {'name':name,'ticks':tick,'note_ons':note_ons,'note_min':note_min,'note_max':note_max,
            'channels':sorted(channels),'programs':programs,'tempos':tempos}

def ticks_to_seconds(max_tick, division, tempo_events):
    if division & 0x8000:
        fps = 256 - ((division >> 8) & 0xff); tpf = division & 0xff
        return max_tick / (fps * tpf) if fps and tpf else 0.0
    ppqn = division; current_us = 500000; last_tick = 0; seconds = 0.0
    for tick, us, _ in sorted(tempo_events, key=lambda x:x[0]):
        if tick < last_tick: continue
        seconds += (tick-last_tick) * current_us / 1_000_000.0 / ppqn
        last_tick = tick; current_us = us
    return seconds + (max_tick-last_tick) * current_us / 1_000_000.0 / ppqn

def inspect(path):
    data = Path(path).read_bytes()
    fmt, ntrks, division, start = parse_header(data)
    tracks=[]; tempos=[]; max_tick=0; chans=set(); percussion=False
    for idx, _, _, _, body in iter_tracks(data, start, ntrks):
        info=parse_track(body, division); info['index']=idx+1; tracks.append(info)
        tempos.extend(info['tempos']); max_tick=max(max_tick, info['ticks'])
        chans.update(info['channels']); percussion = percussion or 9 in info['channels']
    first_bpm = tempos[0][2] if tempos else 120.0
    return {'format':fmt,'tracks':tracks,'track_count':len(tracks),'division':division,
            'ppqn':None if division & 0x8000 else division,'initial_bpm':first_bpm,
            'tempo_event_count':len(tempos),'duration_seconds':ticks_to_seconds(max_tick,division,tempos),
            'channels':sorted(chans),'has_percussion_channel':percussion}

def speed_copy(src, dst, speed):
    if not (0.05 <= speed <= 8.0): raise ValueError('Speed must be between 0.05 and 8.0')
    data = bytearray(Path(src).read_bytes())
    _, ntrks, _, start = parse_header(data)
    out=bytearray(data[:start]); changed=0; has_any_tempo=inspect(src)['tempo_event_count']>0; first_track=True
    for _, _, _, _, body in iter_tracks(data, start, ntrks):
        body=bytes(body); pos=0; running=None; rebuilt=bytearray()
        while pos < len(body):
            ev_start=pos
            _, pos, _ = read_vlq(body, pos)
            if pos >= len(body): rebuilt.extend(body[ev_start:]); break
            status=body[pos]
            if status < 0x80:
                if running is None: raise ValueError('Invalid running status')
                status=running
            else:
                pos += 1
                if 0x80 <= status <= 0xEF: running=status
                else: running=None
            if status == 0xFF:
                if pos >= len(body): raise ValueError('Truncated meta event')
                meta_type=body[pos]; pos+=1
                length, payload_start, _ = read_vlq(body, pos)
                payload_end=payload_start+length
                if payload_end > len(body): raise ValueError('Truncated meta event')
                rebuilt.extend(body[ev_start:payload_start])
                payload=body[payload_start:payload_end]
                if meta_type==0x51 and length==3:
                    us=int.from_bytes(payload,'big')
                    rebuilt.extend(max(1,min(0xFFFFFF,int(round(us/speed)))).to_bytes(3,'big')); changed+=1
                else: rebuilt.extend(payload)
                pos=payload_end
            elif status in (0xF0,0xF7):
                length, p2, _ = read_vlq(body,pos); end=p2+length
                if end>len(body): raise ValueError('Truncated SysEx event')
                rebuilt.extend(body[ev_start:end]); pos=end
            elif 0x80 <= status <= 0xEF:
                need=1 if (status & 0xF0) in (0xC0,0xD0) else 2; end=pos+need
                if end>len(body): raise ValueError('Truncated channel event')
                rebuilt.extend(body[ev_start:end]); pos=end
            else: raise ValueError('Unsupported MIDI event')
        if first_track and not has_any_tempo:
            default_us=max(1,min(0xFFFFFF,int(round(500000/speed))))
            rebuilt=bytearray(b'\x00\xff\x51\x03')+default_us.to_bytes(3,'big')+rebuilt; changed+=1
        first_track=False
        out.extend(b'MTrk'); out.extend(len(rebuilt).to_bytes(4,'big')); out.extend(rebuilt)
    Path(dst).write_bytes(out); return changed


def encode_vlq(value):
    if value < 0:
        raise ValueError('Negative variable-length value')
    out=[value & 0x7f]
    value >>= 7
    while value:
        out.append(0x80 | (value & 0x7f))
        value >>= 7
    return bytes(reversed(out))

def channel_summary(info):
    rows=[]
    for ch in info['channels']:
        latest=None
        for t in info['tracks']:
            for tick,c,p in t['programs']:
                if c == ch and (latest is None or tick >= latest[0]):
                    latest=(tick,p)
        p = latest[1] if latest else 0
        rows.append((ch,p))
    return rows

def print_channels(info):
    for ch,p in channel_summary(info):
        label='Percussion' if ch == 9 else GM_NAMES[p]
        print(f'{ch+1}|{p+1}|{label}')

def track_uses_channel(track, target):
    info=parse_track(track, 480)
    return target in info['channels']

def reassign_copy(src, dst, channel, bank, program):
    if not (0 <= channel <= 15):
        raise ValueError('Channel must be 1-16')
    if not (0 <= bank <= 16383):
        raise ValueError('Bank must be 0-16383')
    if not (0 <= program <= 127):
        raise ValueError('Program must be 1-128')

    data=Path(src).read_bytes()
    _, ntrks, division, start=parse_header(data)
    out=bytearray(data[:start])
    injected=False
    changed=0

    for _, _, _, _, body in iter_tracks(data,start,ntrks):
        body=bytes(body)
        uses=track_uses_channel(body,channel)
        pos=0
        running=None
        events=[]
        while pos < len(body):
            delta, pos, _ = read_vlq(body,pos)
            if pos >= len(body):
                break
            status=body[pos]
            if status < 0x80:
                if running is None:
                    raise ValueError('Invalid running status')
                eff=status if False else running
                explicit=False
            else:
                pos+=1
                eff=status
                explicit=True
                if 0x80 <= eff <= 0xEF:
                    running=eff
                else:
                    running=None

            if eff == 0xFF:
                if pos >= len(body):
                    raise ValueError('Truncated meta event')
                meta_type=body[pos]; pos+=1
                length,payload_start,_=read_vlq(body,pos)
                payload_end=payload_start+length
                if payload_end>len(body):
                    raise ValueError('Truncated meta event')
                raw=bytes([0xFF,meta_type])+encode_vlq(length)+body[payload_start:payload_end]
                pos=payload_end
                events.append([delta,raw,False])
            elif eff in (0xF0,0xF7):
                length,payload_start,_=read_vlq(body,pos)
                payload_end=payload_start+length
                if payload_end>len(body):
                    raise ValueError('Truncated SysEx event')
                raw=bytes([eff])+encode_vlq(length)+body[payload_start:payload_end]
                pos=payload_end
                events.append([delta,raw,False])
            elif 0x80 <= eff <= 0xEF:
                ch=eff & 0x0F
                kind=eff & 0xF0
                need=1 if kind in (0xC0,0xD0) else 2
                if pos+need>len(body):
                    raise ValueError('Truncated channel event')
                payload=body[pos:pos+need]; pos+=need
                skip=False
                if ch == channel:
                    if kind == 0xC0:
                        skip=True
                    elif kind == 0xB0 and payload[0] in (0,32):
                        skip=True
                if skip:
                    changed+=1
                    if events:
                        events[-1][0]+=delta
                    else:
                        events.append([delta,b'',True])
                else:
                    events.append([delta,bytes([eff])+payload,False])
            else:
                raise ValueError(f'Unsupported status byte 0x{eff:02X}')

        if uses and not injected:
            bank_msb=(bank >> 7) & 0x7f
            bank_lsb=bank & 0x7f
            setup=[
                bytes([0xB0|channel,0,bank_msb]),
                bytes([0xB0|channel,32,bank_lsb]),
                bytes([0xC0|channel,program])
            ]
            prefix=bytearray()
            for raw in setup:
                prefix.extend(b'\x00'); prefix.extend(raw)
            rebuilt=bytearray(prefix)
            injected=True
        else:
            rebuilt=bytearray()

        pending=0
        for delta,raw,placeholder in events:
            pending += delta
            if placeholder or not raw:
                continue
            rebuilt.extend(encode_vlq(pending)); rebuilt.extend(raw); pending=0
        if pending:
            rebuilt.extend(encode_vlq(pending)); rebuilt.extend(b'\xff\x2f\x00')

        out.extend(b'MTrk')
        out.extend(len(rebuilt).to_bytes(4,'big'))
        out.extend(rebuilt)

    if not injected:
        raise ValueError(f'Channel {channel+1} is not used by this MIDI')
    Path(dst).write_bytes(out)
    return changed + 3


def format_seconds(sec):
    sec=max(0,int(round(sec))); m,s=divmod(sec,60); h,m=divmod(m,60)
    return f'{h:d}:{m:02d}:{s:02d}' if h else f'{m:d}:{s:02d}'

def print_text(info):
    print(f"MIDI format:       {info['format']}")
    print(f"Tracks:            {info['track_count']}")
    if info['ppqn'] is not None: print(f"Ticks/quarter:     {info['ppqn']}")
    print(f"Initial tempo:     {info['initial_bpm']:.1f} BPM")
    print(f"Tempo changes:     {info['tempo_event_count']}")
    print(f"Approx. duration:  {format_seconds(info['duration_seconds'])}")
    print(f"Channels used:     {', '.join(str(c+1) for c in info['channels']) or 'None'}")
    print(f"Percussion ch. 10: {'Yes' if info['has_percussion_channel'] else 'No'}")
    print('\nTRACKS\n------')
    max_notes=max([t['note_ons'] for t in info['tracks']]+[1])
    for t in info['tracks']:
        name=t['name'] or f"Track {t['index']}"
        bar='#'*int(round((t['note_ons']/max_notes)*24)) if t['note_ons'] else ''
        ch=','.join(str(c+1) for c in t['channels']) or '-'
        rng='-' if t['note_min'] is None else f"{t['note_min']}-{t['note_max']}"
        print(f"{t['index']:>2}. {name[:28]:<28} ch {ch:<8} notes {t['note_ons']:<6} range {rng:<7} {bar}")
        if t['programs']:
            seen=[]
            for _,c,p in t['programs']:
                item=f"ch {c+1}: {GM_NAMES[p]} (program {p+1})"
                if item not in seen: seen.append(item)
            print('    Programs: '+', '.join(seen[:8]))


SLOT_OFFSETS={'A':256,'B':512,'C':768}

def list_soundfont_presets(sf):
    proc=subprocess.run(['fluidsynth','-n',sf],input='fonts\nquit\n',text=True,capture_output=True)
    sfid=1
    for line in proc.stdout.splitlines():
        parts=line.strip().split(None,1)
        if len(parts)==2 and parts[0].isdigit():
            sfid=int(parts[0]); break
    proc=subprocess.run(['fluidsynth','-n',sf],input=f'inst {sfid}\nquit\n',text=True,capture_output=True)
    rows=[]
    for line in proc.stdout.splitlines():
        s=line.strip()
        if '-' not in s: continue
        first,*rest=s.split(None,1)
        if not rest or '-' not in first: continue
        a,b=first.split('-',1)
        if a.isdigit() and b.isdigit():
            rows.append((int(a),int(b),rest[0].strip()))
    return rows

def apply_assignments(src,dst,assignments,offsets=False):
    current=Path(src)
    temp_paths=[]
    try:
        for i,a in enumerate(assignments):
            out=Path(str(dst)+f'.stage{i}')
            bank=int(a['bank']) + (SLOT_OFFSETS[a['slot']] if offsets else 0)
            reassign_copy(current,out,int(a['channel'])-1,bank,int(a['program']))
            if current != Path(src):
                temp_paths.append(current)
            current=out
        shutil.copyfile(current,dst)
    finally:
        for p in temp_paths+[current]:
            if p != Path(src) and p != Path(dst):
                try: Path(p).unlink()
                except FileNotFoundError: pass

def interactive_assign(midi,outdir,slots,result_file=''):
    info=inspect(midi)
    channels=channel_summary(info)
    used={c+1 for c,_ in channels}
    assignments={}
    while True:
        print('\nInstrument Reassignment')
        print('=======================')
        print('MIDI:',Path(midi).name)
        print('\nChannels:')
        for ch,p in channels:
            label='Percussion' if ch==9 else GM_NAMES[p]
            active=assignments.get(ch+1)
            extra=f" -> slot {active['slot']} / {active['name']}" if active else ''
            print(f'  {ch+1:2d}. {label} (program {p+1}){extra}')
        print('\n1. Assign channel')
        print('2. Clear channel assignment')
        print('3. Save modified MIDI')
        print('4. Cancel')
        choice=input('\nChoose an option: ').strip()
        if choice=='1':
            try: ch=int(input('Channel (1-16): ').strip())
            except ValueError: print('Invalid channel.'); continue
            if ch not in used: print('That channel is not used by this MIDI.'); continue
            slot=input('SoundFont slot (A/B/C): ').strip().upper()
            sf=slots.get(slot)
            if slot not in ('A','B','C') or not sf or not Path(sf).is_file():
                print('That SoundFont slot is empty.'); continue
            presets=list_soundfont_presets(sf)
            if not presets: print('No presets could be read from that SoundFont.'); continue
            print(f'\nPresets in slot {slot}:')
            for i,(bank,program,name) in enumerate(presets,1):
                print(f'{i:3d}. bank {bank:3d}  program {program:3d}  {name}')
            try: pick=int(input('Preset number (0 cancels): ').strip())
            except ValueError: print('Invalid preset.'); continue
            if pick==0: continue
            if not 1 <= pick <= len(presets): print('Invalid preset.'); continue
            bank,program,name=presets[pick-1]
            assignments[ch]={'channel':ch,'slot':slot,'bank':bank,'program':program,'name':name,'soundfont':sf}
        elif choice=='2':
            try: ch=int(input('Channel to clear: ').strip())
            except ValueError: continue
            assignments.pop(ch,None)
        elif choice=='3':
            if not assignments:
                print('No assignments to save.'); continue
            stem=Path(midi).stem+' - reassigned'
            outdir=Path(outdir); outdir.mkdir(parents=True,exist_ok=True)
            out=outdir/(stem+'.mid'); counter=2
            while out.exists():
                out=outdir/(f'{stem} ({counter}).mid'); counter+=1
            ordered=[assignments[k] for k in sorted(assignments)]
            apply_assignments(midi,out,ordered,offsets=False)
            sidecar=Path(str(out)+'.fslmap')
            sidecar.write_text(json.dumps({'version':1,'assignments':ordered},indent=2),encoding='utf-8')
            if result_file:
                Path(result_file).write_text(str(out),encoding='utf-8')
            print('Saved modified MIDI:',out)
            return 0
        elif choice=='4':
            return 1
        else:
            print('Invalid option.')

def prepare_playback(midi,target,selected_sf):
    sidecar=Path(str(midi)+'.fslmap')
    if not sidecar.is_file():
        print('plain'); print(midi); print(selected_sf); return
    data=json.loads(sidecar.read_text(encoding='utf-8'))
    assignments=data.get('assignments',[])
    if not assignments:
        print('plain'); print(midi); print(selected_sf); return
    proc=subprocess.run(['fluidsynth','--help'],text=True,capture_output=True)
    help_text=proc.stdout+proc.stderr
    if '--bank-offset' not in help_text:
        slots={a.get('slot') for a in assignments}
        if len(slots)==1:
            slot=next(iter(slots))
            sf=next((a.get('soundfont','') for a in assignments if a.get('slot')==slot), '')
            if sf and Path(sf).is_file():
                print('plain'); print(midi); print(sf); return
        raise ValueError('Multi-SoundFont playback requires FluidSynth 2.5.0 or newer')
    apply_assignments(midi,target,assignments,offsets=True)
    print('mapped'); print(target)
    if selected_sf and Path(selected_sf).is_file():
        print('-b'); print('0'); print(selected_sf)
    seen=set()
    for a in assignments:
        slot=a['slot']; sf=a.get('soundfont','')
        if slot in seen or not sf or not Path(sf).is_file(): continue
        seen.add(slot)
        print('-b'); print(str(SLOT_OFFSETS[slot])); print(sf)

def main():
    ap=argparse.ArgumentParser()
    sub=ap.add_subparsers(dest='cmd',required=True)
    p=sub.add_parser('inspect'); p.add_argument('midi'); p.add_argument('--json',action='store_true')
    p=sub.add_parser('speed'); p.add_argument('input'); p.add_argument('output'); p.add_argument('multiplier',type=float)
    p=sub.add_parser('channels'); p.add_argument('midi')
    p=sub.add_parser('reassign'); p.add_argument('input'); p.add_argument('output'); p.add_argument('channel',type=int); p.add_argument('bank',type=int); p.add_argument('program',type=int)
    p=sub.add_parser('assign-interactive'); p.add_argument('midi'); p.add_argument('outdir'); p.add_argument('--slot-a',default=''); p.add_argument('--slot-b',default=''); p.add_argument('--slot-c',default=''); p.add_argument('--result-file',default='')
    p=sub.add_parser('prepare-playback'); p.add_argument('midi'); p.add_argument('target'); p.add_argument('selected_sf')
    args=ap.parse_args()
    try:
        if args.cmd=='inspect':
            info=inspect(args.midi)
            print(json.dumps(info,indent=2) if args.json else '',end='') if args.json else print_text(info)
        elif args.cmd=='speed':
            print(speed_copy(args.input,args.output,args.multiplier))
        elif args.cmd=='channels':
            print_channels(inspect(args.midi))
        elif args.cmd=='reassign':
            print(reassign_copy(args.input,args.output,args.channel-1,args.bank,args.program-1))
        elif args.cmd=='assign-interactive':
            raise SystemExit(interactive_assign(args.midi,args.outdir,{'A':args.slot_a,'B':args.slot_b,'C':args.slot_c},args.result_file))
        elif args.cmd=='prepare-playback':
            prepare_playback(args.midi,args.target,args.selected_sf)
    except Exception as e:
        print(f'Error: {e}',file=sys.stderr); sys.exit(1)
if __name__=='__main__': main()
