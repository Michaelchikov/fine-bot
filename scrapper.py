# pylint: disable=too-few-public-methods

import datetime
import math
import re
import logging

import requests
from anticaptchaofficial.imagecaptcha import imagecaptcha
from bs4 import BeautifulSoup, element

from config import Config
from models import Media, Protocol, ProtocolStatus, MediaType


class Scrapper:
    """
    BS Scrapper.
    """

    def __init__(self, config: Config):
        self._config = config

        self._logger = logging.getLogger(__name__)
        self._session = requests.Session()

        if config.session_id:
            self._session.cookies.set("PHPSESSID", config.session_id)

        if not self._is_authenticated():
            self._config.session_id = self._authenticate()
            if not self._config.session_id:
                raise Exception("Unable to authenticate. Check logs for details")

    def _is_authenticated(self) -> bool:
        url = f"{self._config.base_url}/protocols.php"
        response = self._session.get(url)

        if response.url == url:
            self._logger.info("Client is authenticated")
            return True

        self._logger.info("Client is not authenticated")
        return False

    def _authenticate(self):
        self._logger.info("Sending authentication request")
        self._session.cookies.clear()

        page = self._session.get(self._config.base_url)
        soup = BeautifulSoup(page.text, "html.parser")
        captcha_img = soup.find("img", id="captcha_code_img")
        csrf_token = soup.find("input", {"name": "csrf_token"})

        captcha_img_response = self._session.get(captcha_img["src"])
        with open("/tmp/captcha.png", "wb") as file_handler:
            file_handler.write(captcha_img_response.content)

        solver = imagecaptcha()
        solver.set_key(self._config.anti_captcha_key)
        solver.set_soft_id(self._config.anti_captcha_soft_id)
        solver.set_case(False)

        self._logger.info("Sending captcha solving request")
        captcha_text = solver.solve_and_return_solution("/tmp/captcha.png")
        if captcha_text == 0:
            self._logger.error("Could not get captcha solution")
            return None

        self._logger.debug("Captcha solution received: %s", captcha_text)

        headers = {
            "Content-Type": "application/x-www-form-urlencoded",
            "Origin": "https://videos.police.ge",
            "Referer": "https://videos.police.ge/index.php?lang=ge",
            "User-Agent": (
                "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko)"
                "Chrome/108.0.0.0 Safari/537.36"
            ),
        }

        response = self._session.post(
            f"{self._config.base_url}/submit-index.php",
            headers=headers,
            data={
                "protocolNo": "",
                "personalNo": "",
                "documentNo": self._config.document_number,
                "vehicleNo2": self._config.vehicle_number,
                "captcha_code": captcha_text,
                "lang": "ge",
                "csrf_token": csrf_token["value"],
            },
        )

        if response.url.find("protocols.php") != -1:
            self._logger.info("Client have been authenticated successfully")
            session_id = self._session.cookies.get("PHPSESSID")
            self._logger.debug("Session ID received: %s", session_id)
            return session_id

        soup = BeautifulSoup(response.text, "html.parser")
        warning = soup.find("div", {"class": ["value", "warning"]})

        self._logger.error(
            "Unable to authenticate client. Reason: %s",
            warning.text if warning is not None else "",
        )
        return None

    def get_protocols(self) -> list[Protocol]:
        """
        Scrape protocols.
        """

        self._logger.info("Getting client protocols")

        page = self._session.get(f"{self._config.base_url}/protocols.php")
        soup = BeautifulSoup(page.text, "html.parser")
        rows = soup.find("div", {"class": "grid"}).find_all("div", {"class": "row"})

        def process_protocol_row(row: element.Tag) -> Protocol:
            media_link = row.find("a")
            if media_link:
                protocol = self._get_protocol_info(media_link)
                protocol.media = self._get_protocol_media(media_link["href"])

                return protocol

            return self._get_protocol_info(row)

        return list(map(process_protocol_row, rows))

    def _get_protocol_info(self, row: element.Tag) -> Protocol:
        cols = row.find_all("span", {"class": "col"})

        def format_date(tag: element.Tag) -> datetime.date:
            descendants = list(tag.descendants)
            raw_date = descendants[0].text

            return datetime.datetime.strptime(raw_date, "%d.%m.%Y").date()

        def format_money_element(tag: element.Tag) -> int:
            descendants = list(tag.descendants)
            if len(descendants) > 0:
                raw_amount = descendants[0]
                float_amount = float(raw_amount[:-4]) * 100

                return math.ceil(float_amount)

            return 0

        def format_status(tag: element.Tag) -> ProtocolStatus:
            if tag.text == "გადახდილია დროულად":
                return ProtocolStatus.PAID_ON_TIME

            if tag.text == "გადაუხდელია":
                return ProtocolStatus.UNPAID

            return ProtocolStatus.UNKNOWN

        receipt_and_numbers = list(
            filter(lambda x: isinstance(x, str), cols[1].descendants)
        )

        self._logger.info(
            "Getting information about protocol %s/%s",
            receipt_and_numbers[1],
            receipt_and_numbers[0],
        )

        return Protocol(
            protocol_number=receipt_and_numbers[0],
            car_state_number=receipt_and_numbers[1],
            date=format_date(cols[2]),
            violation_code=cols[3].text,
            amount=format_money_element(cols[4]),
            status=format_status(cols[6]),
        )

    def _get_protocol_media(self, media_page_url: str) -> list[Media]:
        self._logger.info("Getting media from page: /%s", media_page_url)

        page = self._session.get(f"{self._config.base_url}/{media_page_url}")
        soup = BeautifulSoup(page.text, "html.parser")
        wrapper = (
            soup.find("div", id="content")
            .find("div")
            .find_all("div", recursive=False)[1]
        )

        def create_media_blob(tag: element.Tag) -> Media | None:
            try_img = tag.find("img")
            if try_img is not None:
                url = f"{try_img['src']}"
                media_type = MediaType.PNG
            else:
                result = re.search(r"'oggvideo-(.*)\.ogg';src2", str(tag))
                url = f"oggvideo-{result.groups()[0]}.ogg"
                media_type = MediaType.OGG

            self._logger.info("Getting media item from: /%s", url)
            response = self._session.get(f"{self._config.base_url}/{url}")
            return Media(blob=response.content, type=media_type)

        return list(map(create_media_blob, wrapper.find_all("div", recursive=False)))
