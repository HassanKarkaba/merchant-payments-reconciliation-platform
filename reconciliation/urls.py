from django.urls import path
from . import views
app_name='reconciliation'
urlpatterns=[
 path('operations/imports/new/',views.imports_new,name='import_new'),
 path('operations/imports/from-simulator/',views.from_simulator,name='from_simulator'),
 path('operations/imports/<uuid:import_id>/',views.import_detail,name='import_detail'),
 path('operations/imports/<uuid:import_id>/<str:action>/',views.import_action,name='import_action'),
 path('operations/reconciliation/',views.workspace,name='workspace'),
 path('operations/reconciliation/run/',views.run,name='run'),
 path('operations/matches/new/',views.match_new,name='match_new'),
 path('operations/matches/<uuid:group_id>/reverse/',views.match_reverse,name='match_reverse'),
 path('operations/exceptions/<uuid:exception_id>/',views.exception_detail,name='exception_detail'),
 path('operations/exceptions/<uuid:exception_id>/<str:action>/',views.exception_action,name='exception_action'),
 path('operations/adjustments/<uuid:proposal_id>/<str:decision>/',views.adjustment_decide,name='adjustment_decide'),
 path('operations/periods/',views.periods,name='periods'),
 path('operations/periods/new/',views.period_new,name='period_new'),
 path('operations/periods/<uuid:period_id>/<str:action>/',views.period_action,name='period_action'),
]
