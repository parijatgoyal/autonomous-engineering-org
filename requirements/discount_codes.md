# Requirement: Discount codes at checkout

As a shopper I want to enter a promotional discount code at checkout so that I pay less.

- Supported codes: `SAVE10` (10% off the subtotal) and `FLAT5` (5.00 off the subtotal).
- Codes are entered by humans, so matching must ignore case and surrounding whitespace.
- An unknown code must be rejected with a clear error, not silently ignored.
- The discount applies before tax.
- The order total can never go below zero.
- Orders without a code must be priced exactly as today.
