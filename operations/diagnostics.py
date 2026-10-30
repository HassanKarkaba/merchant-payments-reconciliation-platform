"""Bounded safe diagnostics are separate from mandatory financial audit."""
import json
import logging
import threading
_lock=threading.Lock()
_sink_failures=0
_ALLOWED={'error_class','operation_id','job_id','merchant_id','duration_ms','retry_state','attempts'}

def emit(event,**values):
    global _sink_failures
    safe={key:str(value)[:100] for key,value in values.items() if key in _ALLOWED}
    safe['event']=str(event)[:80]
    try:logging.getLogger('mprp').warning(json.dumps(safe,sort_keys=True,separators=(',',':')))
    except Exception:
        with _lock:_sink_failures+=1

def sink_failure_count():
    with _lock:return _sink_failures
