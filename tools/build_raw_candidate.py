"""Build source-backed candidate bars without changing or releasing original data."""
import argparse
import json
from pathlib import Path
import sys
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'src'))
from raw_candidate import build_candidate

if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--daily',type=Path,required=True)
    parser.add_argument('--evidence',type=Path,required=True)
    parser.add_argument('--output',type=Path,required=True)
    args=parser.parse_args()
    manifest=build_candidate(args.daily,args.evidence,args.output)
    print(json.dumps({'status':manifest['release_status'],**manifest['counts'],'unresolved_responses':len(manifest['rejected_responses'])}))
