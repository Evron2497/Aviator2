# Aviator M-Pesa STK Push integration

The old external deposit-page link has been removed. The DEPOSIT button now opens an in-app M-Pesa form and calls Safaricom Daraja STK Push.

## Environment variables

Copy `.env.example` to your environment and set:

- `MPESA_ENV=sandbox` for development
- `MPESA_CONSUMER_KEY`
- `MPESA_CONSUMER_SECRET`
- `MPESA_SHORTCODE`
- `MPESA_PASSKEY`
- `MPESA_CALLBACK_URL`
- `MPESA_TRANSACTION_TYPE=CustomerPayBillOnline` for PayBill, or `CustomerBuyGoodsOnline` for a Till
- `FLASK_SECRET_KEY`

The callback must be reachable from the internet. Safaricom's current Daraja documentation states that production callback URLs must use HTTPS and that the API is asynchronous. The callback used by this app is `/api/mpesa/callback`.

## Install

```bash
pip install -r requirements.txt
```

## Run

```bash
python app.py
```

## Payment flow

1. User clicks **DEPOSIT**.
2. User enters amount and M-Pesa number.
3. Flask requests a Daraja OAuth token.
4. Flask sends an STK Push.
5. User enters the M-Pesa PIN on the phone.
6. Safaricom calls `/api/mpesa/callback`.
7. The callback marks the transaction successful and credits the user's balance exactly once.
8. The browser polls the transaction status and refreshes the balance.

Do not put Daraja consumer keys, consumer secrets, or the STK passkey in the HTML/JavaScript.
