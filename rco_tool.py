from flask import Flask, request, send_file, render_template_string
import struct, os, io, json, zipfile

app = Flask(__name__)

HTML = """
<!DOCTYPE html>
<html>
<head>
<style>
* { margin:0; padding:0; box-sizing:border-box; }
body { background:#1a1a2e; color:#eee; font-family:monospace; padding:30px; }
h1 { color:#64b4ff; margin-bottom:20px; }
.drop { border:2px dashed #444; padding:40px; text-align:center; margin-bottom:20px; cursor:pointer; }
.drop:hover, .drop.drag { border-color:#64b4ff; }
button { background:#64b4ff; color:#000; border:none; padding:10px 20px; cursor:pointer; font-family:monospace; margin:5px; }
button:hover { background:#8cd0ff; }
#file-list { margin-top:20px; max-height:60vh; overflow-y:auto; }
.file-row { display:flex; align-items:center; gap:10px; padding:8px; border-bottom:1px solid #333; }
.file-row .size { color:#888; font-size:12px; }
.file-row .type { color:#6f6; font-size:12px; }
a { color:#64b4ff; text-decoration:none; }
a:hover { text-decoration:underline; }
#status { margin-top:15px; color:#aaa; }
.tab { display:none; }
.tab.active { display:block; }
.tabs { margin-bottom:20px; }
.tabs button.active { background:#fff; }
input[type=file] { margin:10px 0; }
</style>
</head>
<body>
<h1>RCO Tool</h1>
<div class="tabs">
    <button class="active" onclick="showTab('extract')">Extract</button>
    <button onclick="showTab('compile')">Compile</button>
</div>

<div id="tab-extract" class="tab active">
    <div class="drop" id="drop-extract" onclick="document.getElementById('f-extract').click()">
        Drop .rco file here or click to browse
    </div>
    <input type="file" id="f-extract" accept=".rco" style="display:none">
    <div id="status"></div>
    <button id="dl-all" style="display:none" onclick="location='/download-all/' + window._rcoName">Download All (.zip)</button>
    <div id="file-list"></div>
</div>

<div id="tab-compile" class="tab">
    <p>Upload original RCO + modified files to rebuild.</p>
    <input type="file" id="f-orig" accept=".rco">
    <br>
    <input type="file" id="f-replace" multiple>
    <br>
    <button onclick="doCompile()">Compile RCO</button>
    <div id="compile-status"></div>
</div>

<script>
function showTab(name){
    document.querySelectorAll('.tab').forEach(t=>t.classList.remove('active'));
    document.querySelectorAll('.tabs button').forEach(b=>b.classList.remove('active'));
    document.getElementById('tab-'+name).classList.add('active');
    event.target.classList.add('active');
}

const drop = document.getElementById('drop-extract');
const fExtract = document.getElementById('f-extract');
drop.ondragover = e => { e.preventDefault(); drop.classList.add('drag'); };
drop.ondragleave = () => drop.classList.remove('drag');
drop.ondrop = e => { e.preventDefault(); drop.classList.remove('drag'); uploadExtract(e.dataTransfer.files[0]); };
fExtract.onchange = () => { if(fExtract.files[0]) uploadExtract(fExtract.files[0]); };

async function uploadExtract(file){
    document.getElementById('status').textContent = 'Parsing...';
    const fd = new FormData();
    fd.append('rco', file);
    const r = await fetch('/extract', {method:'POST', body:fd});
    const data = await r.json();
    if(data.error){ document.getElementById('status').textContent = data.error; return; }
    document.getElementById('status').textContent = `${data.files.length} files found (${data.total_size} bytes)`;
    let html = '';
    for(const f of data.files){
        html += `<div class="file-row">
            <span class="type">${f.type}</span>
            <a href="/download/${encodeURIComponent(data.name)}/${f.index}" target="_blank">${f.name}</a>
            <span class="size">${f.size}b</span>
        </div>`;
    }
    document.getElementById('file-list').innerHTML = html;
    window._rcoName = encodeURIComponent(data.name);
    document.getElementById('dl-all').style.display = 'inline-block';
}

async function doCompile(){
    const orig = document.getElementById('f-orig').files[0];
    const reps = document.getElementById('f-replace').files;
    if(!orig || !reps.length){ document.getElementById('compile-status').textContent='Need both files'; return; }
    const fd = new FormData();
    fd.append('original', orig);
    for(const f of reps) fd.append('replacements', f);
    document.getElementById('compile-status').textContent = 'Compiling...';
    const r = await fetch('/compile', {method:'POST', body:fd});
    const data = await r.json();
    if(data.error){ document.getElementById('compile-status').textContent = data.error; return; }
    document.getElementById('compile-status').innerHTML = `Done! <a href="/download-output">Download ${data.name}</a> (${data.size}b)`;
}
</script>
</body>
</html>
"""

extracted = {}
extracted_data = {}

MAX_FILES = 5000

def identify_type(chunk):
    if chunk[:4] == b'\x89PNG': return 'PNG'
    if chunk[:5] == b'<?xml' or b'<?xml' in chunk[:200]: return 'XML'
    if chunk[:4] == b'RIFF': return 'WAV'
    if chunk[:3] == b'ID3': return 'MP3'
    if chunk[:2] in (b'\xff\xfb', b'\xff\xf3', b'\xff\xfc'): return 'MP3'
    if chunk[:4] == b'OGG\s': return 'OGG'
    if chunk[:4] == b'ftyp': return 'MP4'
    if chunk[:2] == b'BM': return 'BMP'
    if chunk[:4] == b'DDS\x1A': return 'DDS'
    if len(chunk) > 4:
        sz = struct.unpack_from('<I', chunk, 0)[0]
        if sz == len(chunk) or sz == len(chunk) - 4:
            return 'VAG'
    return 'BIN'

def carve_blob(blob):
    """Single-pass: find all PNG/XML boundaries, then split."""
    if not blob:
        return []

    # Collect all magic byte positions in one pass using find()
    positions = set()
    i = 1
    while i < len(blob):
        p = blob.find(b'\x89PNG', i)
        if p != -1 and p > 0:
            positions.add(p)
            i = p + 1
        x = blob.find(b'<?xml', i)
        if x != -1 and x > 0:
            positions.add(x)
            i = x + 1
        # If neither found, we're done
        if p == -1 and x == -1:
            break

    # Build sorted boundary list
    boundaries = [0] + sorted(positions) + [len(blob)]

    files = []
    for idx in range(len(boundaries) - 1):
        s, e = boundaries[idx], boundaries[idx + 1]
        chunk = blob[s:e]
        if not chunk:
            continue
        files.append((identify_type(chunk), s, len(chunk)))
        if len(files) >= MAX_FILES:
            break

    return files

@app.route('/')
def index():
    return render_template_string(HTML)

@app.route('/extract', methods=['POST'])
def extract():
    f = request.files.get('rco')
    if not f: return {'error': 'No file'}
    data = f.read()
    if data[:4] != b'RCOF': return {'error': 'Not an RCO file'}
    name = f.filename
    data_off = struct.unpack_from('<I', data, 0x40)[0]
    data_size = struct.unpack_from('<I', data, 0x4C)[0]
    if data_size == 0:
        data_size = struct.unpack_from('<I', data, 0x44)[0]
    if data_off + data_size > len(data):
        data_size = len(data) - data_off
    blob = data[data_off:data_off+data_size]
    files = carve_blob(blob)
    extracted[name] = files
    extracted_data[name] = data
    return {'name': name, 'total_size': data_size, 'files': [
        {'index': i, 'name': f'file_{i:03d}', 'type': t, 'size': s}
        for i, (t, o, s) in enumerate(files)
    ]}

@app.route('/download/<rco_name>/<int:idx>')
def download(rco_name, idx):
    if rco_name not in extracted: return 'Not found', 404
    files = extracted[rco_name]
    if idx >= len(files): return 'Not found', 404
    t, off, size = files[idx]
    data = extracted_data[rco_name]
    data_off = struct.unpack_from('<I', data, 0x40)[0]
    chunk = data[data_off+off:data_off+off+size]
    ext = {'PNG':'.png','XML':'.xml','VAG':'.vag','BIN':'.bin','MP3':'.mp3','OGG':'.ogg','DDS':'.dds'}.get(t,'.bin')
    return send_file(io.BytesIO(chunk), as_attachment=True, download_name=f'file_{idx:03d}{ext}')

@app.route('/download-all/<rco_name>')
def download_all(rco_name):
    if rco_name not in extracted: return 'Not found', 404
    files = extracted[rco_name]
    data = extracted_data[rco_name]
    data_off = struct.unpack_from('<I', data, 0x40)[0]
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, 'w', zipfile.ZIP_DEFLATED) as zf:
        for i, (t, off, size) in enumerate(files):
            chunk = data[data_off+off:data_off+off+size]
            ext = {'PNG':'.png','XML':'.xml','VAG':'.vag','BIN':'.bin','MP3':'.mp3','OGG':'.ogg','DDS':'.dds'}.get(t,'.bin')
            zf.writestr(f'file_{i:03d}{ext}', chunk)
    del extracted[rco_name]
    del extracted_data[rco_name]
    buf.seek(0)
    return send_file(buf, as_attachment=True, download_name=rco_name.replace('.rco','')+'_extracted.zip')

@app.route('/compile', methods=['POST'])
def compile_rco():
    orig = request.files.get('original')
    reps = request.files.getlist('replacements')
    if not orig: return {'error': 'No original RCO'}
    data = bytearray(orig.read())
    if data[:4] != b'RCOF': return {'error': 'Not an RCO'}
    data_off = struct.unpack_from('<I', data, 0x40)[0]
    data_size = struct.unpack_from('<I', data, 0x4C)[0]
    if data_size == 0:
        data_size = struct.unpack_from('<I', data, 0x44)[0]
    if data_off + data_size > len(data):
        data_size = len(data) - data_off
    blob = bytes(data[data_off:data_off+data_size])
    files = carve_blob(blob)

    rep_map = {}
    for r in reps:
        rep_map[r.filename] = r.read()

    new_parts = []
    for i, (t, off, size) in enumerate(files):
        fname = f'file_{i:03d}'
        matched = False
        for rname, rdata in rep_map.items():
            if fname in rname or f'{i:03d}' in rname:
                new_parts.append(rdata)
                matched = True
                break
        if not matched:
            new_parts.append(blob[off:off+size])

    new_blob = b''.join(new_parts)
    new_data = bytes(data[:data_off]) + new_blob + bytes(data[data_off+data_size:])
    ba = bytearray(new_data)
    struct.pack_into('<I', ba, 0x4C, len(new_blob))
    if struct.unpack_from('<I', ba, 0x44)[0] != 0:
        struct.pack_into('<I', ba, 0x44, len(new_blob))
    new_data = bytes(ba)

    outname = orig.filename.replace('.rco', '_out.rco')
    extracted_data['__output__'] = new_data
    return {'name': outname, 'size': len(new_data)}

@app.route('/download-output')
def download_output():
    data = extracted_data.get('__output__')
    if not data: return 'Nothing to download', 404
    return send_file(io.BytesIO(data), as_attachment=True, download_name='output.rco')

if __name__ == '__main__':
    app.run(host='127.0.0.1', port=5000)
