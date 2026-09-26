from django.contrib import admin

from finance import models

for model in (
    models.BankConnection,
    models.BankAccount,
    models.AccountSyncer,
    models.Transaction,
    models.BalanceSnapshot,
    models.Category,
    models.CategoryRule,
    models.Budget,
    models.RecurringPayment,
):
    admin.site.register(model)
