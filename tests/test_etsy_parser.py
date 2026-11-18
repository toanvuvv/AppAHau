"""Tests for Etsy shipping address parsing across countries."""
from etsy_parser import _extract_shipping_address, _html_extract_shipping_address
from bs4 import BeautifulSoup


def test_extract_shipping_address_us_plain_text():
    text = (
        "Shipping address\n"
        "John Doe\n"
        "123 Main St\n"
        "Springfield, IL 62704\n"
        "United States\n\n"
    )
    buyer_name, address = _extract_shipping_address(text)
    assert buyer_name == "John Doe"
    assert address["address_1"] == "123 Main St"
    assert address["city"] == "Springfield"
    assert address["state"] == "IL"
    assert address["zipcode"] == "62704"
    assert address["country"] == "United States"


def test_extract_shipping_address_au_plain_text():
    text = (
        "Shipping address\n"
        "Sam Walker\n"
        "42 Example Road\n"
        "BUTLER WA 6036\n"
        "Australia\n\n"
    )
    buyer_name, address = _extract_shipping_address(text)
    assert buyer_name == "Sam Walker"
    assert address["address_1"] == "42 Example Road"
    assert address["city"] == "BUTLER"
    assert address["state"] == "WA"
    assert address["zipcode"] == "6036"
    assert address["country"] == "Australia"


def test_extract_shipping_address_ca_plain_text():
    text = (
        "Shipping address\n"
        "Jane Smith\n"
        "55 Front St W\n"
        "Toronto ON M5V 2T6\n"
        "Canada\n\n"
    )
    buyer_name, address = _extract_shipping_address(text)
    assert buyer_name == "Jane Smith"
    assert address["address_1"] == "55 Front St W"
    assert address["city"] == "Toronto"
    assert address["state"] == "ON"
    assert address["zipcode"] == "M5V2T6"
    assert address["country"] == "Canada"


def test_extract_shipping_address_de_plain_text_zip_first():
    text = (
        "Shipping address\n"
        "Max Mustermann\n"
        "Musterstrasse 10\n"
        "08393 Meerane\n"
        "Germany\n\n"
    )
    buyer_name, address = _extract_shipping_address(text)
    assert buyer_name == "Max Mustermann"
    assert address["address_1"] == "Musterstrasse 10"
    assert address["city"] == "Meerane"
    assert address["state"] == ""
    assert address["zipcode"] == "08393"
    assert address["country"] == "Germany"


def test_extract_shipping_address_de_plain_text_zip_last():
    text = (
        "Shipping address\n"
        "Max Mustermann\n"
        "Musterstrasse 10\n"
        "Meerane 08393\n"
        "Germany\n\n"
    )
    buyer_name, address = _extract_shipping_address(text)
    assert buyer_name == "Max Mustermann"
    assert address["city"] == "Meerane"
    assert address["zipcode"] == "08393"


def test_extract_shipping_address_with_address_2():
    text = (
        "Shipping address\n"
        "Alice Parker\n"
        "221B Baker Street\n"
        "Apt 4\n"
        "London NW1 6XE\n"
        "United Kingdom\n\n"
    )
    buyer_name, address = _extract_shipping_address(text)
    assert buyer_name == "Alice Parker"
    assert address["address_1"] == "221B Baker Street"
    assert address["address_2"] == "Apt 4"
    assert address["city"] == "London"
    assert address["zipcode"] == "NW16XE"
    assert address["country"] == "United Kingdom"


def test_html_extract_shipping_address_au():
    html = """
    <address>
      <span>Sam Walker</span><br>
      <span>42 Example Road</span><br>
      <span>BUTLER</span> <span>WA</span> <span>6036</span><br>
      <span>Australia</span>
    </address>
    """
    soup = BeautifulSoup(html, "html.parser")
    buyer_name, address = _html_extract_shipping_address(soup)
    assert buyer_name == "Sam Walker"
    assert address["address_1"] == "42 Example Road"
    assert address["city"] == "BUTLER"
    assert address["state"] == "WA"
    assert address["zipcode"] == "6036"
    assert address["country"] == "Australia"
