# scripts/seed_state.py  — run LOCALLY once to create a logged-in storage state
# 1) pip install playwright
# 2) playwright install chromium
# 3) python scripts/seed_state.py
# A Chrome window opens; you log in and solve CAPTCHA manually; press ENTER to save.

from playwright.sync_api import sync_playwright

STATE_OUT = "state.json"  # created in the current working directory

def main():
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=False)  # headed so you can solve the CAPTCHA
        context = browser.new_context(
            viewport={"width": 1400, "height": 900},
            locale="en-US",
            timezone_id="America/New_York",
            user_agent=("Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                        "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"),
        )
        page = context.new_page()
        page.goto("https://www.tcgplayer.com/login?returnUrl=https://www.tcgplayer.com/", wait_until="load")
        print("\nA browser window is open. Log in to TCGplayer and complete any CAPTCHA/MFA.")
        input("When you see you are logged in, press ENTER here to save state.json... ")

        # quick sanity: go to homepage
        page.goto("https://www.tcgplayer.com/", wait_until="load")
        context.storage_state(path=STATE_OUT)
        print(f"Saved logged-in session to {STATE_OUT}")
        context.close(); browser.close()

if __name__ == "__main__":
    main()
