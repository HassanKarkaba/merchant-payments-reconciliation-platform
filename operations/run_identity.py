"""One immutable run identity per process, bound explicitly at runtime startup."""
import os
import threading
from dataclasses import dataclass
from django.conf import settings


class RunIdentityError(Exception):
    pass


@dataclass(frozen=True,repr=False)
class RunIdentity:
    environment_id:str
    main_instance_id:str
    run_token:str
    dispatch_epoch:int
    simulator_instance_id:str
    process_id:int


_bound_identity=None
_binding_lock=threading.Lock()


def _snapshot(control):
    if not control or str(control.environment_id)!=str(settings.MPRP_ENVIRONMENT_ID) or not control.dispatch_epoch or not control.simulator_instance_id:
        raise RunIdentityError('The local run has not been prepared')
    return RunIdentity(str(control.environment_id),str(control.main_instance_id),str(control.run_token),int(control.dispatch_epoch),str(control.simulator_instance_id),os.getpid())


def bind_process_identity(control=None):
    """Called by web/worker startup; management processes bind once on first use."""
    global _bound_identity
    if control is None:
        from .models import Control
        control=Control.objects.filter(pk=1).first()
    current=_snapshot(control)
    with _binding_lock:
        if _bound_identity is None:_bound_identity=current
        if _bound_identity!=current:
            raise RunIdentityError('This process belongs to a replaced local run')
        return _bound_identity


def is_bound():
    return _bound_identity is not None


def is_current(control,bind_if_unbound=True):
    try:
        if _bound_identity is None and not bind_if_unbound:return False
        return bind_process_identity(control)==_snapshot(control)
    except RunIdentityError:
        return False


def control_scope(control):
    """A stale probe/heartbeat cannot qualify or change a newer run's control."""
    identity=_bound_identity
    if identity is None:
        return {'pk':control.pk,'run_token':control.run_token,'dispatch_epoch':control.dispatch_epoch,'main_instance_id':control.main_instance_id,'simulator_instance_id':control.simulator_instance_id,'environment_id':control.environment_id}
    return {'pk':control.pk,'run_token':identity.run_token,'dispatch_epoch':identity.dispatch_epoch,'main_instance_id':identity.main_instance_id,'simulator_instance_id':identity.simulator_instance_id,'environment_id':identity.environment_id}
