"""Local development launcher; accepts conventional preview runner arguments."""
import argparse
import os
import uvicorn

parser=argparse.ArgumentParser()
parser.add_argument('--host',default='127.0.0.1')
parser.add_argument('--port',type=int,default=8000)
parser.add_argument('--strictPort',action='store_true')
args=parser.parse_args()
# Explicit local configuration only; never infer trusted origins from request headers.
if os.path.exists('.preview-origin') and os.environ.get('WASLA_ENV','development')=='development':
    with open('.preview-origin') as source: os.environ['WASLA_ORIGIN']=source.read().strip()
uvicorn.run('wasla.main:app',host=args.host,port=args.port)
