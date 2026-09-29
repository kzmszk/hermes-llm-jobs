#!/usr/bin/env python3
"""Issue an app credential without printing it. Requires Wrangler administrator login."""
import argparse, hashlib, json, os, re, secrets, subprocess, tempfile
from pathlib import Path
ROOT=Path(__file__).resolve().parent
p=argparse.ArgumentParser();p.add_argument('app');p.add_argument('--types',nargs='+',choices=['quiz.grade','document.summarize','illustration.svg','video.generate'],required=True);args=p.parse_args()
if not re.fullmatch('[a-z0-9-]{1,60}',args.app):p.error('Use an app ID containing lowercase letters, digits, and hyphens.')
private=ROOT/'.private/apps';private.mkdir(parents=True,exist_ok=True,mode=0o700)
key=secrets.token_hex(32);digest=hashlib.sha256(key.encode()).hexdigest()
sql="INSERT INTO llm_apps(id,key_hash,allowed_types) VALUES('%s','%s','%s') ON CONFLICT(id) DO UPDATE SET key_hash=excluded.key_hash,allowed_types=excluded.allowed_types,enabled=1;"%(args.app,digest,json.dumps(args.types))
with tempfile.NamedTemporaryFile(mode='w',suffix='.sql') as f:
    f.write(sql);f.flush()
    subprocess.run(['node','node_modules/wrangler/bin/wrangler.js','d1','execute','DB','--remote','--file',f.name],cwd=ROOT,check=True)
path=private/(args.app+'.json')
fd=os.open(path,os.O_WRONLY|os.O_CREAT|os.O_TRUNC,0o600)
with os.fdopen(fd,'w') as f:json.dump({'url':'https://hermes-llm-jobs.kazumasa.workers.dev','appId':args.app,'apiKey':key},f)
print('Credential saved to '+str(path)+' (value not displayed).')
