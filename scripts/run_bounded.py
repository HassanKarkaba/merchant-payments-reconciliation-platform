"""Finite subprocess runner used by the verification container."""
import subprocess
import sys
try:result=subprocess.run(sys.argv[2:],timeout=int(sys.argv[1]));raise SystemExit(result.returncode)
except subprocess.TimeoutExpired:raise SystemExit('Verification exceeded its declared finite deadline')
