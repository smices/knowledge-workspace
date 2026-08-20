import asyncio
from io import BytesIO

import pytest
from fastapi import HTTPException, UploadFile

from app.auth import Principal
from app.main import upload_document


def test_non_admin_cannot_assign_a_knowledge_base():
    file = UploadFile(filename="example.txt", file=BytesIO(b"test"))
    principal = Principal("reader", "tenant", frozenset({"reader"}))

    with pytest.raises(HTTPException) as error:
        asyncio.run(upload_document(file, None, "测试资料", principal))

    assert error.value.status_code == 403
