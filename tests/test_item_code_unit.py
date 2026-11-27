import pytest
from pydantic import ValidationError

from app.models.models import Product, _give_free_item_code
from app.schemas.schemas import ProductCreate, ProductUpdate


class _Result:
    def __init__(self, value):
        self.value = value

    def scalar_one(self):
        return self.value

    def first(self):
        return self.value


class _Connection:
    """Answers the two queries in turn: the sequence's next number, then who holds it."""

    def __init__(self, *answers):
        self.answers = list(answers)

    def execute(self, _statement):
        return _Result(self.answers.pop(0))


def test_typed_item_code_is_kept():
    product = Product(item_code=112244)
    _give_free_item_code(None, _Connection(), product)
    assert product.item_code == 112244


def test_automatic_code_skips_numbers_typed_by_hand():
    product = Product()
    _give_free_item_code(None, _Connection(100007, ("taken",), 100008, None), product)
    assert product.item_code == 100008


@pytest.mark.parametrize("bad", [0, -5, 10**12])
def test_item_code_must_be_a_short_positive_number(bad):
    with pytest.raises(ValidationError):
        ProductCreate(sku="A", name="A", item_code=bad)
    with pytest.raises(ValidationError):
        ProductUpdate(item_code=bad)


def test_item_code_is_optional():
    assert ProductCreate(sku="A", name="A").item_code is None
    assert "item_code" not in ProductUpdate(name="B").model_dump(exclude_unset=True)
