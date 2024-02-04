#!/usr/bin/env python3
# ruff: noqa: E402

import logging
import time
from datetime import datetime
from typing import List

import httpx
from fastapi import FastAPI
from g2p_cash_transfer_bridge_core.models.disburse import (
    SingleDisburseStatusEnum,
)
from g2p_cash_transfer_bridge_core.models.msg_header import MsgStatusEnum
from g2p_cash_transfer_bridge_core.models.orm.payment_list import PaymentListItem
from g2p_cash_transfer_bridge_core.services.id_translate_service import (
    IdTranslateService,
)
from openg2p_fastapi_common.config import Settings as BaseSettings
from openg2p_fastapi_common.errors import BaseAppException
from openg2p_fastapi_common.service import BaseService
from openg2p_fastapi_common.utils.ctx_thread import CTXThread
from pydantic import BaseModel
from pydantic_settings import SettingsConfigDict
from sqlalchemy import and_, create_engine, select
from sqlalchemy.orm import Session


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="gctb_mojaloop_sdk_", env_file=".env", extra="allow"
    )
    openapi_title: str = "GCTB Mojaloop SDK Adapter Payment Backend"
    openapi_description: str = """
    Payment Backend for Mojaloop SDK Adapter  of G2P Cash Transfer Bridge.

    ***********************************
    Further details goes here
    ***********************************
    """
    openapi_version: str = "0.1.0"
    payment_backend_name: str = "mojaloop"
    db_dbname: str = "gctbdb"
    db_driver: str = "postgresql"

    dsbmt_loop_intial_delay_secs: int = 30
    dsbmt_loop_interval_secs: int = 10
    dsbmt_loop_filter_backend_name: bool = True
    dsbmt_loop_filter_status: List[str] = ["rcvd", "fail"]
    translate_id_to_fa: bool = True

    api_timeout: int = 10
    transfers_url: str = "https://bank1.mec.openg2p.net/api/outbound/transfers"
    payer_id_type: str = "ACCOUNT_ID"
    payer_id_value: str = "1212121212"
    payee_id_type: str = "ACCOUNT_ID"
    payer_display_name: str = "Government Treasury Bank"
    transfer_note: str = "GCTB benefit transfer"
    translate_id_to_fa: bool = True


_config = Settings.get_config()
_logger = logging.getLogger(_config.logging_default_logger_name)


class ReferenceIdStatus(BaseModel):
    txn_id: str
    ref_id: str
    status: MsgStatusEnum


class MojaloopSdkPaymentBackendService(BaseService):
    def __init__(self, name="", **kwargs):
        super().__init__(name if name else _config.payment_backend_name, **kwargs)
        self._id_translate_service = IdTranslateService.get_component()
        self.disburse_loop_killed = False
        self.disburse_loop_thread: CTXThread = None

    @property
    def id_translate_service(self):
        if not self._id_translate_service:
            self._id_translate_service = IdTranslateService.get_component()
        return self._id_translate_service

    def post_init(self):
        self.disburse_loop_thread = CTXThread(target=self.disburse_loop)
        self.disburse_loop_thread.start()

    def disburse_loop(self):
        time.sleep(_config.dsbmt_loop_intial_delay_secs)
        dbengine = create_engine(_config.db_datasource, echo=_config.db_logging)
        with Session(dbengine, expire_on_commit=False) as session:
            while not self.disburse_loop_killed:
                db_response = []
                stmt = select(PaymentListItem)
                if (
                    _config.dsbmt_loop_filter_backend_name
                    and _config.dsbmt_loop_filter_status
                ):
                    stmt = stmt.where(
                        and_(
                            PaymentListItem.backend_name
                            == _config.payment_backend_name,
                            PaymentListItem.status.in_(
                                [
                                    MsgStatusEnum[status]
                                    for status in _config.dsbmt_loop_filter_status
                                ]
                            ),
                        )
                    )
                elif _config.dsbmt_loop_filter_backend_name:
                    stmt = stmt.where(
                        PaymentListItem.backend_name == _config.payment_backend_name
                    )
                elif _config.dsbmt_loop_filter_status:
                    stmt = stmt.where(
                        PaymentListItem.status.in_(
                            [
                                MsgStatusEnum[status]
                                for status in _config.dsbmt_loop_filter_status
                            ]
                        )
                    )
                stmt = stmt.order_by(PaymentListItem.id.asc())
                result = session.execute(stmt)

                db_response = list(result.scalars())
                if db_response:
                    _logger.info(
                        "GCTB Mojaloop - processing payment from payment list."
                    )
                    self.disburse(db_response, session)
                else:
                    _logger.info(
                        "GCTB Mojaloop - no records found in payment list table."
                    )
                time.sleep(_config.dsbmt_loop_interval_secs)

    def disburse(self, payments: List[PaymentListItem], session: Session):
        for payment in payments:
            payee_acc_no = ""
            if _config.translate_id_to_fa:
                try:
                    payee_acc_no = self.id_translate_service.translate_sync(
                        [
                            payment.to_fa,
                        ],
                        loop_sleep=0,
                        max_retries=100,
                    )
                    if payee_acc_no:
                        payee_acc_no = payee_acc_no[0]
                except Exception:
                    _logger.exception("Mojaloop Payment Failed couldnot get FA from ID")
            else:
                payee_acc_no = payment.to_fa
            data = {
                "homeTransactionId": payment.request_id,
                "from": {
                    "idType": _config.payer_id_type,
                    "idValue": _config.payer_id_value,
                    "displayName": _config.payer_display_name,
                },
                "to": {
                    "idType": _config.payee_id_type,
                    "idValue": self.get_payee_id_value_from_payee_fa(payee_acc_no),
                },
                "currency": payment.currency,
                "amount": float(payment.amount),
                "note": _config.transfer_note,
                "transactionType": "TRANSFER",
                "amountType": "SEND",
            }
            try:
                response = httpx.post(
                    _config.transfers_url,
                    json=data,
                    timeout=_config.api_timeout,
                )
                _logger.info(
                    "Mojaloop SDK Payment Transfer response: %s", response.content
                )
                response.raise_for_status()

                # TODO: Do Status check rather than hardcoding
                payment.updated_at = datetime.utcnow()
                payment.status = MsgStatusEnum.succ
            except Exception:
                _logger.exception("Mojaloop Payment Failed with unknown reason")
                payment.updated_at = datetime.utcnow()
                payment.status = MsgStatusEnum.rjct
                payment.error_code = SingleDisburseStatusEnum.rjct_payment_failed
                payment.error_msg = "Mojaloop Payment Failed with unknown reason"

            session.commit()

    def get_payee_id_value_from_payee_fa(self, fa: str) -> str:
        return fa[fa.find(":") + 1 : fa.rfind("@")]


from gctb_translate_id_fa.app import Initializer as TranslateIdInitializer
from openg2p_common_g2pconnect_id_mapper.app import (
    Initializer as G2pConnectMapperInitializer,
)
from openg2p_fastapi_common.app import Initializer
from openg2p_fastapi_common.ping import PingController


class PingController(PingController):
    async def get_ping(self):
        res = await super().get_ping()
        payment_backend = MojaloopSdkPaymentBackendService.get_component()
        if not payment_backend.disburse_loop_thread.is_alive():
            raise BaseAppException("GCTB-MLP-700", "Disbursement Loop is not running")
        return res


class PaymentBackendInitializer(Initializer):
    def initialize(self, **kwargs):
        super().initialize(**kwargs)
        self.payment_backend = MojaloopSdkPaymentBackendService()
        PingController().post_init()

    def init_db(self):
        pass

    async def fastapi_app_startup(self, app: FastAPI):
        self.payment_backend.post_init()

    async def fastapi_app_shutdown(self, app: FastAPI):
        self.payment_backend.disburse_loop_killed = True


if __name__ == "__main__":
    main_init = PaymentBackendInitializer()
    G2pConnectMapperInitializer()
    TranslateIdInitializer()
    main_init.main()
