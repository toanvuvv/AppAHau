"""Parse Etsy sale notification emails into structured order data."""
import re

from bs4 import BeautifulSoup

US_COUNTRIES = {"united states", "usa", "us", "u.s.a.", "u.s."}
CA_COUNTRIES = {"canada", "ca"}
AU_COUNTRIES = {"australia", "au"}
DE_COUNTRIES = {"germany", "de", "deutschland"}
NL_COUNTRIES = {"netherlands", "the netherlands", "nl", "holland"}
KNOWN_COUNTRIES = US_COUNTRIES | CA_COUNTRIES | AU_COUNTRIES | DE_COUNTRIES | NL_COUNTRIES | {
    "united kingdom",
    "uk",
    "great britain",
    "france",
    "italy",
    "spain",
    "belgium",
    "ireland",
    "sweden",
    "norway",
    "denmark",
    "switzerland",
    "austria",
    "poland",
    "portugal",
    "new zealand",
}


def parse_etsy_sale_email(plain_text, html_body=None):
    """Parse an Etsy 'You made a sale' email.

    Tries HTML parsing first (more reliable for forwarded emails),
    falls back to plain text parsing.

    Args:
        plain_text: Decoded plain text body of the email.
        html_body: Raw HTML body (preferred for parsing).

    Returns:
        dict with order data, or None if email cannot be parsed.
    """
    # Try HTML parsing first — more reliable for forwarded emails
    if html_body:
        result = _parse_from_html(html_body)
        if result and result.get("order_id"):
            return result

    # Fallback to plain text parsing
    if not plain_text:
        return None

    order_id = _extract_order_id(plain_text)
    if not order_id:
        return None

    buyer_name, address = _extract_shipping_address(plain_text)
    items = _extract_items(plain_text, html_body)
    phone = _extract_phone(plain_text)
    date = _extract_date_from_text(plain_text)

    return {
        "date": date,
        "order_id": order_id,
        "buyer_name": buyer_name,
        "address_1": address.get("address_1", ""),
        "address_2": address.get("address_2", ""),
        "city": address.get("city", ""),
        "state": address.get("state", ""),
        "zipcode": address.get("zipcode", ""),
        "country": address.get("country", ""),
        "phone": phone,
        "items": items,
    }


# ---------------------------------------------------------------------------
# HTML-based parsing (primary path)
# ---------------------------------------------------------------------------


def _parse_from_html(html_body):
    """Parse Etsy sale email from HTML body using BeautifulSoup.

    The Etsy email HTML has structured elements:
    - Order ID in <a href="etsy.com/your/orders/ORDER_ID">
    - Shipping address in <address> tag with <span> elements
    - Item details near transaction links
    - Thumbnail in <img src="i.etsystatic.com/.../il_75x75...">
    """
    soup = BeautifulSoup(html_body, "html.parser")

    order_id = _html_extract_order_id(soup, html_body)
    if not order_id:
        return None

    buyer_name, address = _html_extract_shipping_address(soup)
    items = _html_extract_items(soup, html_body)
    phone = _html_extract_phone(soup)
    date = _html_extract_date(soup, html_body)

    return {
        "date": date,
        "order_id": order_id,
        "buyer_name": buyer_name,
        "address_1": address.get("address_1", ""),
        "address_2": address.get("address_2", ""),
        "city": address.get("city", ""),
        "state": address.get("state", ""),
        "zipcode": address.get("zipcode", ""),
        "country": address.get("country", ""),
        "phone": phone,
        "items": items,
    }


def _html_extract_order_id(soup, html_body):
    """Extract order ID from HTML.

    Patterns:
    - <a href="https://www.etsy.com/your/orders/ORDER_ID...">ORDER_ID</a>
    - Text "order number is: ORDER_ID"
    - Subject line "Order #ORDER_ID"
    """
    # Pattern 1: link to etsy.com/your/orders/ORDER_ID
    for a_tag in soup.find_all("a", href=True):
        m = re.search(r"etsy\.com/your/orders/(\d{10,13})", a_tag["href"])
        if m:
            return m.group(1)

    # Pattern 2: regex on raw HTML for "order number is:" text
    m = re.search(r"order number is:?\s*(?:<[^>]*>)*\s*(\d{10,13})", html_body, re.IGNORECASE)
    if m:
        return m.group(1)

    # Pattern 3: "Order #ORDER_ID" in subject or text
    text = soup.get_text()
    m = re.search(r"Order\s*#\s*(\d{10,13})", text)
    if m:
        return m.group(1)

    return None


def _html_extract_shipping_address(soup):
    """Extract buyer name and address from HTML <address> tag.

    Etsy HTML format:
    <address style="font-style:normal">
        <span>Cathy Cusack</span><br>
        <span>1510 Normandy Rd</span><br>
        <span>TAYLORSVILLE</span>, <span>KY</span> <span>40071</span><br>
        <span>United States</span>
    </address>
    """
    addr_tag = soup.find("address")
    if not addr_tag:
        return "", _empty_address()

    # Get all spans inside the address tag
    spans = addr_tag.find_all("span")
    if not spans:
        return "", _empty_address()

    span_texts = [s.get_text(strip=True) for s in spans]

    # The address structure uses <br> to separate logical lines.
    # Reconstruct lines by walking the address tag's contents.
    lines = []
    current_line_parts = []
    for child in addr_tag.descendants:
        if child.name == "br":
            if current_line_parts:
                lines.append(" ".join(current_line_parts).strip())
                current_line_parts = []
        elif child.name == "span":
            current_line_parts.append(child.get_text(strip=True))
    if current_line_parts:
        lines.append(" ".join(current_line_parts).strip())

    # Remove empty lines
    lines = [l for l in lines if l]

    if not lines:
        # Fallback: use span_texts directly
        lines = span_texts

    buyer_name, address = _parse_address_lines(lines)

    # Fallback: some templates split city/state/zip in separate spans without clean <br>.
    if not address["city"] and len(span_texts) >= 4:
        fallback_name, fallback_address = _parse_address_lines(span_texts)
        if fallback_address["city"]:
            return fallback_name, fallback_address

    return buyer_name, address


def _html_extract_items(soup, html_body):
    """Extract product items from HTML.

    Each item block in Etsy HTML has:
    - <a href="etsy.com/transaction/TX_ID">Product Title</a>
    - <img src="i.etsystatic.com/.../il_75x75..."> (thumbnail)
    - "Shop: ShopName" in a div
    - "Transaction ID: TX_ID" in a div
    - "Quantity: N" in a div
    """
    items = []

    # Find all transaction links — each represents an item
    tx_links = []
    for a_tag in soup.find_all("a", href=True):
        m = re.search(r"etsy\.com/transaction/(\d+)", a_tag["href"])
        if m:
            tx_id = m.group(1)
            title = a_tag.get_text(strip=True)
            # Skip non-title links (like "Send them a Convo", images, etc.)
            if title and len(title) > 5 and tx_id not in [t[0] for t in tx_links]:
                tx_links.append((tx_id, title, a_tag))

    for tx_id, title, a_tag in tx_links:
        # Find the item block container — walk up to find the enclosing table/div
        item_container = _find_item_container(a_tag)

        # Thumbnail: find il_75x75 image near this transaction
        thumbnail = _extract_thumbnail(html_body, tx_id)

        # Extract fields from individual <div> texts to avoid joined-text regex issues
        div_texts = _get_div_texts(item_container) if item_container else []

        # Quantity
        quantity = 1
        for dt in div_texts:
            qty_match = re.match(r"Quantity:\s*(\d+)", dt)
            if qty_match:
                quantity = int(qty_match.group(1))
                break

        # Variations — extract from div texts line by line
        variation_1, variation_2 = _extract_variations_from_divs(div_texts, title)

        # Personalization — extract from div texts
        personalized = ""
        for dt in div_texts:
            pm = re.match(r"[Pp]ersonali[sz](?:ation|ed)[:\s]+(.+)", dt)
            if pm:
                personalized = pm.group(1).strip()
                break

        # SKU
        sku = ""
        for dt in div_texts:
            sm = re.match(r"SKU[:\s]+(.+)", dt)
            if sm:
                sku = sm.group(1).strip()
                break

        # Shop name
        shop = ""
        for dt in div_texts:
            shop_m = re.match(r"Shop:\s*(.+)", dt)
            if shop_m:
                shop = shop_m.group(1).strip()
                break

        items.append({
            "title": title,
            "sku": sku,
            "thumbnail": thumbnail,
            "shop": shop,
            "variation_1": variation_1,
            "variation_2": variation_2,
            "quantity": quantity,
            "personalized": personalized,
        })

    if not items:
        items.append({
            "title": "",
            "sku": "",
            "thumbnail": "",
            "shop": "",
            "variation_1": "",
            "variation_2": "",
            "quantity": 1,
            "personalized": "",
        })

    return items


def _get_div_texts(container):
    """Extract text from each direct and nested <div> in the container.

    Returns a list of stripped text strings, one per <div> that has content.
    This avoids the joined-text problem where get_text(" ") merges all divs.
    """
    texts = []
    for div in container.find_all("div"):
        # Only get the div's own text (not nested divs)
        text = div.get_text(strip=True)
        if text:
            texts.append(text)
    return texts


_NON_VARIATION_LABELS = re.compile(
    r"^(Shop|Transaction\s*ID|Quantity|Price|Total|Personali[sz](?:ation|ed)|SKU|"
    r"Order|Shipping|Payment|Subtotal|Tax|Discount|Coupon|Gift\s*wrap|Note|Message|"
    r"Listing\s*ID|Item\s*ID|Sold\s*by|Sold\s*to|Estimated|Expected|Deliver)",
    re.IGNORECASE,
)


def _is_variation_div(text):
    """Return (label, value) if the div text looks like a product variation, else None."""
    m = re.match(r"^([^:]{1,60}):\s*(.+)$", text.strip())
    if not m:
        return None
    label, value = m.group(1).strip(), m.group(2).strip()
    if _NON_VARIATION_LABELS.match(label):
        return None
    if value.startswith("$") or value.startswith("US$"):
        return None
    return label, value


def _extract_variations_from_divs(div_texts, title=""):
    """Extract variation 1 and variation 2 from individual div texts.

    Accepts any 'Label: Value' line that is not a known non-variation field.
    This handles custom labels like 'Unisex shirt size: Tshirt Size S US letter'.
    Skips div texts that contain the item title (titles with colons look like variations).
    """
    variations = []
    for dt in div_texts:
        # Skip divs that contain the product title — titles with colons
        # (e.g. "Custom Daisy Embroidered Converse: Mint Chuck Taylor Shoes")
        # would be falsely matched as a variation by _is_variation_div.
        if title and title in dt:
            continue
        result = _is_variation_div(dt)
        if result:
            _label, value = result
            variations.append(value)
        if len(variations) >= 2:
            break

    variation_1 = variations[0] if len(variations) > 0 else ""
    variation_2 = variations[1] if len(variations) > 1 else ""
    return variation_1, variation_2


def _find_item_container(tag):
    """Walk up from an item tag to find the containing block.

    Returns the nearest ancestor <div> or <table> that contains
    Transaction ID, Quantity, etc.
    """
    current = tag.parent
    for _ in range(15):  # max depth
        if current is None:
            break
        text = current.get_text(" ", strip=True)
        if "Transaction ID:" in text and "Quantity:" in text:
            return current
        current = current.parent
    return tag.parent


def _html_extract_phone(soup):
    """Extract phone number from HTML text."""
    text = soup.get_text()
    phone_match = re.search(r"[Pp]hone[:\s]+([\d\+\-\(\)\s]{7,20})", text)
    if phone_match:
        return phone_match.group(1).strip()
    return ""


def _html_extract_date(soup, html_body):
    """Extract order date from HTML.

    Look for the email date in forwarded headers or subject line.
    """
    text = soup.get_text()

    # Pattern: "Ship by Dec 10" in subject — extract from subject context
    # Look for the original Etsy email date in forwarded message headers
    # "Date: Sun, Dec 7, 2025 at 5:58 AM"
    m = re.search(
        r"Date:\s*\w+,\s*(\w+\s+\d{1,2},\s*\d{4})",
        text,
    )
    if m:
        return m.group(1).strip()

    # Forwarded Vietnamese format: "Date: CN, 22 thg 3, 2026"
    m = re.search(
        r"Date:\s*\w+,\s*(\d{1,2}\s+thg\s+\d{1,2},\s*\d{4})",
        text,
    )
    if m:
        return m.group(1).strip()

    # Subject line: "Ship by Dec 10"
    m = re.search(r"Ship by (\w+ \d{1,2})", text)
    if m:
        return m.group(1).strip()

    return ""


# ---------------------------------------------------------------------------
# Plain-text parsing (fallback)
# ---------------------------------------------------------------------------


def _extract_order_id(text):
    """Extract Etsy order ID from email text."""
    m = re.search(r"order number is:?\s*(\d{10,13})", text, re.IGNORECASE)
    if m:
        return m.group(1)
    m = re.search(r"Order\s*#\s*(\d{10,13})", text)
    if m:
        return m.group(1)
    return None


def _extract_date_from_text(text):
    """Extract date from plain text email."""
    m = re.search(r"Date:\s*\w+,\s*(\w+\s+\d{1,2},\s*\d{4})", text)
    if m:
        return m.group(1).strip()
    m = re.search(r"Ship by (\w+ \d{1,2})", text)
    if m:
        return m.group(1).strip()
    return ""


def _extract_shipping_address(text):
    """Extract buyer name and shipping address from email text."""
    addr_match = re.search(
        r"Shipping address\s*\n(.+?)(?:\n\s*\n|Shipping internationally|Double check|$)",
        text,
        re.DOTALL | re.IGNORECASE,
    )
    if not addr_match:
        addr_match = re.search(
            r"Ship(?:ping)?\s+(?:to|address)\s*\n(.+?)(?:\n\s*\n|$)",
            text,
            re.DOTALL | re.IGNORECASE,
        )

    if not addr_match:
        return "", _empty_address()

    block = addr_match.group(1).strip()
    lines = [l.strip() for l in block.split("\n") if l.strip()]
    return _parse_address_lines(lines)


def _empty_address():
    return {
        "address_1": "",
        "address_2": "",
        "city": "",
        "state": "",
        "zipcode": "",
        "country": "",
    }


def _parse_address_lines(lines):
    """Parse normalized address lines into Etsy address fields."""
    address = _empty_address()
    if not lines:
        return "", address

    clean_lines = [line.strip() for line in lines if line and line.strip()]
    if not clean_lines:
        return "", address

    buyer_name = clean_lines[0]
    remaining = clean_lines[1:]
    if not remaining:
        return buyer_name, address

    country = ""
    if _looks_like_country(remaining[-1]):
        country = remaining.pop()
        address["country"] = country

    locality_line = ""
    street_lines = remaining
    if remaining:
        locality_line, street_lines = _split_street_and_locality(remaining)

    if street_lines:
        address["address_1"] = street_lines[0]
        if len(street_lines) > 1:
            address["address_2"] = street_lines[1]

    city, state, zipcode = _parse_locality_line(locality_line, country)
    address["city"] = city
    address["state"] = state
    address["zipcode"] = zipcode

    return buyer_name, address


def _looks_like_country(line):
    normalized = _normalize_country(line)
    return normalized in KNOWN_COUNTRIES


def _normalize_country(value):
    normalized = value.strip().lower().replace(".", "")
    normalized = re.sub(r"\s+", " ", normalized)
    return normalized


def _split_street_and_locality(lines):
    """Split address lines into street lines and a single locality line."""
    if len(lines) == 1:
        return "", lines

    last = lines[-1]
    prev = lines[-2]
    if _looks_like_postcode_only(prev) and not re.search(r"\d", last):
        return f"{prev} {last}".strip(), lines[:-2]
    if _looks_like_postcode_only(last) and not re.search(r"\d", prev):
        return f"{prev} {last}".strip(), lines[:-2]
    return last, lines[:-1]


def _looks_like_postcode_only(value):
    return bool(re.match(r"^[A-Z0-9][A-Z0-9\- ]{2,9}$", value.strip(), re.IGNORECASE) and re.search(r"\d", value))


def _parse_locality_line(locality_line, country):
    city = ""
    state = ""
    zipcode = ""
    if not locality_line:
        return city, state, zipcode

    line = " ".join(locality_line.replace(",", " ").split())
    normalized_country = _normalize_country(country) if country else ""

    # US: City ST ZIP(5|9)
    m = re.match(r"^(.+?)\s+([A-Z]{2})\s+(\d{5}(?:-\d{4})?)$", line, re.IGNORECASE)
    if m and normalized_country in US_COUNTRIES:
        return m.group(1).strip(), m.group(2).upper(), m.group(3).upper()

    # Canada: City PR A1A 1A1
    m = re.match(r"^(.+?)\s+([A-Z]{2})\s+([A-Z]\d[A-Z]\s?\d[A-Z]\d)$", line, re.IGNORECASE)
    if m and normalized_country in CA_COUNTRIES:
        return m.group(1).strip(), m.group(2).upper(), m.group(3).upper().replace(" ", "")

    # Australia: City ST 4DIGIT
    m = re.match(r"^(.+?)\s+([A-Z]{2,3})\s+(\d{4})$", line, re.IGNORECASE)
    if m and normalized_country in AU_COUNTRIES:
        return m.group(1).strip(), m.group(2).upper(), m.group(3)

    # Netherlands: 4DIGIT 2LETTER City (e.g. "2861 TN BERGAMBACHT")
    if normalized_country in NL_COUNTRIES:
        m = re.match(r"^(\d{4}\s?[A-Z]{2})\s+(.+)$", line, re.IGNORECASE)
        if m:
            return m.group(2).strip(), "", m.group(1).upper().replace(" ", " ")
        # Reversed: City 4DIGIT 2LETTER
        m = re.match(r"^(.+?)\s+(\d{4}\s?[A-Z]{2})$", line, re.IGNORECASE)
        if m:
            return m.group(1).strip(), "", m.group(2).upper().replace(" ", " ")

    # Germany: 5DIGIT City or City 5DIGIT
    if normalized_country in DE_COUNTRIES:
        m = re.match(r"^(\d{5})\s+(.+)$", line)
        if m:
            return m.group(2).strip(), "", m.group(1)
        m = re.match(r"^(.+?)\s+(\d{5})$", line)
        if m:
            return m.group(1).strip(), "", m.group(2)

    # UK-style postcode (e.g. "London NW1 6XE")
    m = re.match(r"^(.+?)\s+([A-Z]{1,2}\d[A-Z\d]?\s?\d[A-Z]{2})$", line, re.IGNORECASE)
    if m:
        return m.group(1).strip(), "", m.group(2).upper().replace(" ", "")

    # Generic fallback: try AU-like / ZIP-first / ZIP-last, else keep full line as city.
    m = re.match(r"^(.+?)\s+([A-Z]{2,3})\s+([A-Z0-9][A-Z0-9\- ]{2,9})$", line, re.IGNORECASE)
    if m and re.search(r"\d", m.group(3)):
        return m.group(1).strip(), m.group(2).upper(), m.group(3).upper().replace(" ", "")

    m = re.match(r"^([A-Z0-9\-]{3,10})\s+(.+)$", line, re.IGNORECASE)
    if m and re.search(r"\d", m.group(1)):
        return m.group(2).strip(), "", m.group(1).upper().replace(" ", "")

    m = re.match(r"^(.+?)\s+([A-Z0-9\-]{3,10})$", line, re.IGNORECASE)
    if m and re.search(r"\d", m.group(2)):
        return m.group(1).strip(), "", m.group(2).upper().replace(" ", "")

    return locality_line.strip(), "", ""


def _extract_items(text, html_body=None):
    """Extract product items from plain text email."""
    items = []

    tx_positions = list(re.finditer(r"Transaction ID:\s*(\d+)", text))

    for i, tx_match in enumerate(tx_positions):
        transaction_id = tx_match.group(1)
        tx_pos = tx_match.start()

        if i + 1 < len(tx_positions):
            block_end = tx_positions[i + 1].start()
        else:
            block_end = len(text)

        if i > 0:
            block_start = tx_positions[i - 1].end()
        else:
            od_match = re.search(r"Order details", text[:tx_pos], re.IGNORECASE)
            block_start = od_match.end() if od_match else 0

        before_tx = text[block_start:tx_pos]
        after_tx = text[tx_pos:block_end]
        full_block = before_tx + after_tx

        title = _extract_product_title_from_block(before_tx)

        qty_match = re.search(r"Quantity:\s*(\d+)", after_tx)
        quantity = int(qty_match.group(1)) if qty_match else 1

        variation_1, variation_2 = _extract_variations(full_block, title)

        personal_match = re.search(
            r"[Pp]ersonali[sz]ation[:\s]+([^\n]+)", full_block
        )
        personalized = personal_match.group(1).strip() if personal_match else ""

        sku_match = re.search(r"SKU[:\s]+([^\n]+)", full_block)
        sku = sku_match.group(1).strip() if sku_match else ""

        shop_match = re.search(r"Shop:\s*([^\n]+)", full_block)
        shop = shop_match.group(1).strip() if shop_match else ""

        thumbnail = ""
        if html_body:
            thumbnail = _extract_thumbnail(html_body, transaction_id)

        items.append({
            "title": title,
            "sku": sku,
            "thumbnail": thumbnail,
            "shop": shop,
            "variation_1": variation_1,
            "variation_2": variation_2,
            "quantity": quantity,
            "personalized": personalized,
        })

    if not items:
        items.append({
            "title": "",
            "sku": "",
            "thumbnail": "",
            "shop": "",
            "variation_1": "",
            "variation_2": "",
            "quantity": 1,
            "personalized": "",
        })

    return items


def _extract_product_title_from_block(before_tx_text):
    """Extract product title from the text block before Transaction ID."""
    lines = before_tx_text.strip().split("\n")

    for i, line in enumerate(lines):
        line_s = line.strip()
        if re.match(r"^Shop:", line_s):
            for j in range(i - 1, -1, -1):
                candidate = lines[j].strip()
                if not candidate:
                    continue
                if candidate.startswith("<http") or candidate.startswith("http"):
                    continue
                if len(candidate) <= 5:
                    continue
                return candidate
            break

    for line in reversed(lines):
        line = line.strip()
        if not line or len(line) <= 10:
            continue
        if line.startswith("<http") or line.startswith("http"):
            continue
        if re.match(r"^(Shop:|Order details|Sell with|Learn|Congratulations|Your order|Shipping|Payment|Double check)", line, re.IGNORECASE):
            continue
        return line

    return ""


def _extract_variations(block, title=""):
    """Extract variation 1 and variation 2 from item block (plain text).

    Scans each line for 'Label: Value' pairs, skipping known non-variation
    fields. This handles custom labels like 'Unisex shirt size: Tshirt Size S'.
    Skips lines that contain the item title (titles with colons look like variations).
    """
    variations = []
    for line in block.splitlines():
        line = line.strip()
        if not line:
            continue
        if title and title in line:
            continue
        result = _is_variation_div(line)
        if result:
            _label, value = result
            variations.append(value)
        if len(variations) >= 2:
            break

    variation_1 = variations[0] if len(variations) > 0 else ""
    variation_2 = variations[1] if len(variations) > 1 else ""
    return variation_1, variation_2


def _extract_thumbnail(html_body, transaction_id):
    """Extract product thumbnail URL from HTML body."""
    if not html_body:
        return ""

    tx_area = ""
    tx_pos = html_body.find(str(transaction_id))
    if tx_pos > 0:
        start = max(0, tx_pos - 2000)
        end = min(len(html_body), tx_pos + 2000)
        tx_area = html_body[start:end]

    search_areas = [tx_area, html_body] if tx_area else [html_body]

    for area in search_areas:
        img_match = re.search(
            r'src=["\']?(https://i\.etsystatic\.com/[^"\'>\s]+il_75x75[^"\'>\s]*)["\']?',
            area,
        )
        if img_match:
            return img_match.group(1)

    return ""


def _extract_phone(text):
    """Extract phone number from email text."""
    phone_match = re.search(
        r"[Pp]hone[:\s]+([\d\+\-\(\)\s]{7,20})", text
    )
    if phone_match:
        return phone_match.group(1).strip()
    return ""
