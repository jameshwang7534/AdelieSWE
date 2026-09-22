"""Session utilities remain testable without a running service."""

from unittest.mock import Mock

import pytest
from sqlalchemy.orm import Session, sessionmaker

from app.db.session import session_scope


def test_session_scope_propagates_errors() -> None:
    factory = Mock(spec=sessionmaker)
    session = Mock(spec=Session)
    factory.begin.return_value.__enter__ = Mock(return_value=session)
    factory.begin.return_value.__exit__ = Mock(return_value=False)
    with pytest.raises(RuntimeError, match="work failed"), session_scope(factory) as active:
        assert active is session
        raise RuntimeError("work failed")
    assert factory.begin.return_value.__exit__.call_args.args[0] is RuntimeError
