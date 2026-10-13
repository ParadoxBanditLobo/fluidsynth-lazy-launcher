#!/usr/bin/env python3
import argparse, json, struct, sys
from pathlib import Path

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
                item=f"ch {c+1}: Program {p}"
                if item not in seen: seen.append(item)
            print('    Programs: '+', '.join(seen[:8]))

def main():
    ap=argparse.ArgumentParser()
    sub=ap.add_subparsers(dest='cmd',required=True)
    p=sub.add_parser('inspect'); p.add_argument('midi'); p.add_argument('--json',action='store_true')
    p=sub.add_parser('speed'); p.add_argument('input'); p.add_argument('output'); p.add_argument('multiplier',type=float)
    args=ap.parse_args()
    try:
        if args.cmd=='inspect':
            info=inspect(args.midi)
            print(json.dumps(info,indent=2) if args.json else '',end='') if args.json else print_text(info)
        else: print(speed_copy(args.input,args.output,args.multiplier))
    except Exception as e:
        print(f'Error: {e}',file=sys.stderr); sys.exit(1)
if __name__=='__main__': main()
