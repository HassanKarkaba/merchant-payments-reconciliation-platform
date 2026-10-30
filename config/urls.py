from django.urls import path, include
from operations import views
urlpatterns=[path('',views.home,name='home'),path('accounts/',include('accounts.urls')),path('m/',include('merchants.urls')),path('',include('payments.urls')),path('',include('settlements.urls')),path('',include('disputes.urls')),path('',include('reconciliation.urls')),path('',include('operations.urls')),path('',include('accounting.urls'))]
