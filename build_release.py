"""Build deterministic development ZIPs without AWS access."""
from pathlib import Path
from zipfile import ZipFile, ZipInfo, ZIP_DEFLATED
import hashlib
root=Path(__file__).resolve().parent
out=root/'dist';out.mkdir(exist_ok=True)
for name,files in [('seven-hills-lambda-v0.8.0.zip',['lambda_function.py','patient_store.py']),('seven-hills-web-v0.8.0.zip',['index.html','app.js'])]:
    target=out/name
    with ZipFile(target,'w',compression=ZIP_DEFLATED) as z:
        for f in files:
            info=ZipInfo(f,(2026,1,1,0,0,0));info.compress_type=ZIP_DEFLATED
            z.writestr(info,(root/f).read_bytes())
    with ZipFile(target) as z:
        assert z.testzip() is None
        assert set(z.namelist())==set(files)
    print(hashlib.sha256(target.read_bytes()).hexdigest(),target.name)
