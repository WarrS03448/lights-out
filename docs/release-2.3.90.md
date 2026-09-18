# Lights Out 2.3.90

The Lights Out login form now includes **Forgot password?**. Enter the account
email, verify the six-digit email code, then choose and confirm a new password.
Successful recovery returns to sign-in and revokes earlier Lights Out sessions.
The same option is available in Settings when signing in to connect an account
or confirming its password. Settings recovery preserves the current Steam
session and returns to account sign-in before any connection can proceed.

Codes expire after 15 minutes, allow five wrong attempts and can be verified once.
Verification grants five minutes to finish the password change. The server
enforces both steps, including expiry, concurrent requests and account changes.
Recovery screens and emails support all seven app languages. Existing password
requirements remain six to 128 characters.

Resending, cancellation, expired codes and uncertain network outcomes have clear
recovery paths. Email existence stays private in the public API response and
the verification endpoint performs equivalent storage work for absent accounts.

Steam sign-in, account linking, gameplay and match rules are unchanged. This
community project is not affiliated with or endorsed by Reissad Studio. Lights
Out and its authored gamemode downloads are not cheats.

Verification uses isolated account/mail fixtures, real Redis Lua execution,
client state tests and browser checks in seven languages at 800/1280 widths.
No real player's password was reset during testing.
