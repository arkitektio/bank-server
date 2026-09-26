from datalayer import base_models
from datalayer.scalars import ByteCount
from strawberry.experimental import pydantic


@pydantic.input(model=base_models.RequestBigFileUploadInput, all_fields=True)
class RequestBigFileUploadInput:
    """
    Docstring for RequestMediaUploadInput
    """

    file_size: ByteCount | None = None


@pydantic.input(model=base_models.FinishBigFileUploadInput, all_fields=True)
class FinishBigFileUploadInput:
    """
    Docstring for FinishMediaUploadInput
    """

    pass


@pydantic.input(model=base_models.RequestBigFileAccessInput, all_fields=True)
class RequestBigFileAccessInput:
    """
    Docstring for RequestBigFileAccessInput
    """

    pass


