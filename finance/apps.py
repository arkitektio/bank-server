from django.apps import AppConfig


class FinanceConfig(AppConfig):
    """Bank accounts, transactions, categories, budgets and the stats over them."""

    default_auto_field = "django.db.models.BigAutoField"
    name = "finance"

    def ready(self) -> None:
        import embeddings.checks  # noqa: F401  registers the model/width system checks
        from finance import scheduled  # noqa: F401  registers the rekuest actions (and, through
        # bank_server.service, the model signals) in every process
