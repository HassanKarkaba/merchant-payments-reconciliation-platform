from django.urls import path
from . import views

urlpatterns = [
    path('health/live/', views.live),
    path('v1/capabilities/', views.capabilities),
    path('v1/control/handshake/', views.handshake),
    path('v1/control/restore-release/', views.restoration_release),
    path('v1/resolve-key/', views.resolve),
    path('v1/operations/<str:operation_key>/', views.operation),
    path('v1/accounts/<uuid:account_id>/', views.account),
    path('v1/accounts/<uuid:account_id>/retire/', views.retire),
    path('v1/accounts/<uuid:account_id>/retirement/', views.retirement),
    path('v1/payments/<uuid:object_id>/', views.payment),
    path('v1/objects/<uuid:object_id>/', views.payment),
    path('v1/disputes/<uuid:object_id>/', views.payment),
    path('v1/history/<uuid:object_id>/', views.history),
    path('v1/events/', views.events),
    path('v1/inventory/', views.inventory),
    path('v1/inventory/sources/', views.source_inventory),
    path('v1/events/ack/', views.acknowledge),
    path('v1/statements/', views.statement),
    path('v1/statements/<uuid:manifest_id>/', views.manifest),
    path('v1/statements/<uuid:manifest_id>/csv/', views.manifest, {'csv_output': True}),
]
for kind in sorted(__import__('processor_sim.domain', fromlist=['KINDS']).KINDS):
    urlpatterns.append(path(f'v1/{kind}/', views.mutation, {'kind': kind}))
