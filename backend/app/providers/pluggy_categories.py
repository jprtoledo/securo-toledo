"""Pluggy category id → Securo default-category internal key.

Pluggy's GET /categories catalog as of 2026-10-16 (ported from the toledo-finance
seeds). Used to categorize synced rows by ``provider_category_id`` before falling
back to the text map ``PLUGGY_CATEGORY_MAP`` (RFC toledo-features §5.3, rule 4).
The internal keys resolve to the workspace's own category through every
language variant of the default names, so it works whatever language the
categories were created in. Ids Pluggy adds later fall back to the text map.
"""

PLUGGY_CATEGORY_ID_MAP: dict[str, str] = {
    "01000000": "salary",  # Income
    "01010000": "salary",  # Salary
    "01020000": "salary",  # Retirement
    "01030000": "salary",  # Entrepreneurial activities
    "01040000": "salary",  # Government aid
    "01050000": "salary",  # Non-recurring income
    "02000000": "taxes",  # Loans and financing
    "02010000": "taxes",  # Late payment and overdraft costs
    "02020000": "taxes",  # Interests charged
    "02030000": "taxes",  # Financing
    "02030001": "taxes",  # Real estate financing
    "02030002": "taxes",  # Vehicle financing
    "02030003": "taxes",  # Student loan
    "02040000": "taxes",  # Loans
    "03000000": "investments",  # Investments
    "03010000": "investments",  # Automatic investment
    "03020000": "investments",  # Fixed income
    "03030000": "investments",  # Mutual funds
    "03040000": "investments",  # Variable income
    "03050000": "investments",  # Margin
    "03060000": "investments",  # Proceeds interests and dividends
    "03070000": "investments",  # Pension
    "04000000": "transfers",  # Same person transfer
    "04010000": "transfers",  # Same person transfer - CASH
    "04020000": "transfers",  # Same person transfer - PIX
    "04030000": "transfers",  # Same person transfer - TED
    "05000000": "transfers",  # Transfers
    "05010000": "transfers",  # Transfer - Bank Slip
    "05020000": "transfers",  # Transfer - Cash
    "05030000": "transfers",  # Transfer - Check
    "05040000": "transfers",  # Transfer - DOC
    "05050000": "transfers",  # Transfer - Foreign Exchange
    "05060000": "transfers",  # Transfer - Internal
    "05070000": "transfers",  # Transfer - PIX
    "05080000": "transfers",  # Transfer - TED
    "05090000": "transfers",  # Third party transfers
    "05090001": "transfers",  # Third party transfer - Bank Slip
    "05090002": "transfers",  # Third party transfer - Debit Card
    "05090003": "transfers",  # Third party transfer - DOC
    "05090004": "transfers",  # Third party transfer - PIX
    "05090005": "transfers",  # Third party transfer - TED
    "05100000": "transfers",  # Credit card payment
    "06000000": "other",  # Legal obligations
    "06010000": "other",  # Blocked balances
    "06020000": "other",  # Alimony
    "07000000": "other",  # Services
    "07010000": "housing",  # Telecommunications
    "07010001": "housing",  # Internet
    "07010002": "housing",  # Mobile
    "07010003": "housing",  # TV
    "07020000": "education",  # Education
    "07020001": "education",  # Online Courses
    "07020002": "education",  # University
    "07020003": "education",  # School
    "07020004": "education",  # Kindergarten
    "07030000": "health",  # Wellness and fitness
    "07030001": "health",  # Gyms and fitness centers
    "07030002": "health",  # Sports practice
    "07030003": "health",  # Wellness
    "07040000": "leisure",  # Tickets
    "07040001": "leisure",  # Stadiums and arenas
    "07040002": "leisure",  # Landmarks and museums
    "07040003": "leisure",  # Cinema, theater and concerts
    "08000000": "shopping",  # Shopping
    "08010000": "shopping",  # Online shopping
    "08020000": "shopping",  # Electronics
    "08030000": "shopping",  # Pet supplies and vet
    "08040000": "shopping",  # Clothing
    "08050000": "shopping",  # Kids and toys
    "08060000": "shopping",  # Bookstore
    "08070000": "shopping",  # Sports goods
    "08080000": "shopping",  # Office supplies
    "08090000": "salary",  # Cashback
    "09000000": "subscriptions",  # Digital services
    "09010000": "leisure",  # Gaming
    "09020000": "subscriptions",  # Video streaming
    "09030000": "subscriptions",  # Music streaming
    "10000000": "groceries",  # Groceries
    "11000000": "food",  # Food and drinks
    "11010000": "food",  # Eating out
    "11020000": "food",  # Food delivery
    "12000000": "transport",  # Travel
    "12010000": "transport",  # Airport and airlines
    "12020000": "transport",  # Accomodation
    "12030000": "transport",  # Mileage programs
    "12040000": "transport",  # Bus tickets
    "13000000": "donations",  # Donations
    "14000000": "leisure",  # Gambling
    "14010000": "leisure",  # Lottery
    "14020000": "leisure",  # Online bet
    "15000000": "taxes",  # Taxes
    "15010000": "taxes",  # Income taxes
    "15020000": "taxes",  # Taxes on investments
    "15030000": "taxes",  # Tax on financial operations
    "16000000": "taxes",  # Bank fees
    "16010000": "taxes",  # Account fees
    "16020000": "taxes",  # Wire transfer fees and ATM fees
    "16030000": "taxes",  # Credit card fees
    "17000000": "housing",  # Housing
    "17010000": "housing",  # Rent
    "17020000": "housing",  # Utilities
    "17020001": "housing",  # Water
    "17020002": "housing",  # Electricity
    "17020003": "housing",  # Gas
    "17030000": "housing",  # Houseware
    "17040000": "taxes",  # Urban land and building tax
    "18000000": "health",  # Healthcare
    "18010000": "health",  # Dentist
    "18020000": "health",  # Pharmacy
    "18030000": "health",  # Optometry
    "18040000": "health",  # Hospital clinics and labs
    "19000000": "transport",  # Transportation
    "19010000": "transport",  # Taxi and ride-hailing
    "19020000": "transport",  # Public transportation
    "19030000": "transport",  # Car rental
    "19040000": "transport",  # Bicycle
    "19050000": "transport",  # Automotive
    "19050001": "transport",  # Gas stations
    "19050002": "transport",  # Parking
    "19050003": "transport",  # Tolls and in vehicle payment
    "19050004": "transport",  # Vehicle ownership taxes and fees
    "19050005": "transport",  # Vehicle maintenance
    "19050006": "transport",  # Traffic tickets
    "20000000": "other",  # Insurance
    "21000000": "leisure",  # Leisure
    "99999999": "other",  # Other
}


# Pluggy category text → default category name (the original pt-BR map),
# used when a row carries no known category id.
PLUGGY_CATEGORY_MAP = {
    "Eating out": "Alimentação",
    "Restaurants": "Alimentação",
    "Food": "Alimentação",
    "Groceries": "Mercado",
    "Supermarkets": "Mercado",
    "Pharmacy": "Saúde",
    "Health": "Saúde",
    "Taxi and ride-hailing": "Transporte",
    "Transport": "Transporte",
    "Gas": "Transporte",
    "Travel": "Transporte",
    "Housing": "Moradia",
    "Rent": "Moradia",
    "Utilities": "Moradia",
    "Entertainment": "Lazer",
    "Leisure": "Lazer",
    "Education": "Educação",
    "Subscriptions": "Assinaturas",
    "Online services": "Assinaturas",
    "Transfer": "Transferências",
    "Transfers": "Transferências",
    "Wire transfers": "Transferências",
}
