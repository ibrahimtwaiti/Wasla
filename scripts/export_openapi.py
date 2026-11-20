"""Deterministic documentation export; contains no environment values or data."""
import json
from pathlib import Path
from wasla.main import app
from wasla.api_docs import schema

def main():
    path=Path(__file__).resolve().parent.parent/'docs/openapi.json'
    path.write_text(json.dumps(schema(),ensure_ascii=False,indent=2)+'\n')
    print('OpenAPI exported:',path.name)
if __name__=='__main__':main()
