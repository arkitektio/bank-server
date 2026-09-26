from django.contrib import admin

from datalayer import models

admin.site.register(models.DatalayerStore)
admin.site.register(models.BigFileStore)
