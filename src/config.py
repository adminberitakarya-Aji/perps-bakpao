"""
Konfigurasi agent. Baca dari environment variables (lihat .env.example).
JANGAN commit private key ke git.
"""

import os
from dataclasses import dataclass

from dotenv import load_dotenv
from hyperliquid.utils import constants


@dataclass
class Config:
    # Private key dari API wallet (BUKAN wallet utama). Buat API wallet
    # terpisah lewat app.hyperliquid.xyz/API -> approve agent wallet.
    private_key: str
    # Alamat wallet utama (yang menyimpan dana), dipakai untuk query state.
    account_address: str
    use_testnet: bool = True
    # Alert Telegram (opsional -- kosongkan keduanya = silent mode)
    telegram_bot_token: str = ""
    telegram_chat_id: str = ""

    @property
    def api_url(self) -> str:
        return constants.TESTNET_API_URL if self.use_testnet else constants.MAINNET_API_URL

    @classmethod
    def from_env(cls) -> "Config":
        # Muat .env dari project root (dicari ke atas dari lokasi file ini).
        # Variabel yang sudah di-set di shell tetap menang (tidak di-override).
        load_dotenv()

        private_key = os.environ.get("HL_PRIVATE_KEY")
        account_address = os.environ.get("HL_ACCOUNT_ADDRESS")
        use_testnet = os.environ.get("HL_USE_TESTNET", "true").lower() == "true"

        if not private_key or not account_address:
            raise ValueError(
                "HL_PRIVATE_KEY dan HL_ACCOUNT_ADDRESS wajib di-set. "
                "Copy .env.example ke .env dan isi nilainya."
            )

        return cls(
            private_key=private_key,
            account_address=account_address,
            use_testnet=use_testnet,
            telegram_bot_token=(os.environ.get("TELEGRAM_BOT_TOKEN") or "").strip(),
            telegram_chat_id=(os.environ.get("TELEGRAM_CHAT_ID") or "").strip(),
        )
