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
        env_prefix="gctb_simple_mpesa_", env_file=".env", extra="allow"
    )
    openapi_title: str = "GCTB Simple Mpesa Payment Backend"
    openapi_description: str = """
    Payment Backend for Simple M-pesa of G2P Cash Transfer Bridge.

    ***********************************
    Further details goes here
    ***********************************
    """
    openapi_version: str = "0.1.0"
    payment_backend_name: str = "mpesa"
    db_dbname: str = "gctbdb"
    db_driver: str = "postgresql"

    dsbmt_loop_intial_delay_secs: int = 30
    dsbmt_loop_interval_secs: int = 10
    dsbmt_loop_filter_backend_name: bool = True
    dsbmt_loop_filter_status: List[str] = ["rcvd", "fail"]
    translate_id_to_fa: bool = True

    agent_email: str = "agent.mpesa@openg2p.org"
    agent_password: str = "openg2p123"
    auth_url: str = "https://mpesa.qa.openg2p.net/api/login/agent"
    payment_url: str = "https://mpesa.qa.openg2p.net/api/transaction/deposit"
    api_timeout: int = 10
    customer_type: str = "subscriber"


_config = Settings.get_config()
_logger = logging.getLogger(_config.logging_default_logger_name)


class ReferenceIdStatus(BaseModel):
    txn_id: str
    ref_id: str
    status: MsgStatusEnum


class SimpleMpesaPaymentBackendService(BaseService):
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
                        "GCTB Simple Mpesa - processing payment from payment list."
                    )
                    self.disburse(db_response, session)
                else:
                    _logger.info(
                        "GCTB Simple Mpesa - no records found in payment list table."
                    )
                time.sleep(_config.dsbmt_loop_interval_secs)

    def disburse(self, payments: List[PaymentListItem], session: Session):
        try:
            # from_fa field will be ignored in this payment_backend
            data = {"email": _config.agent_email, "password": _config.agent_password}

            response = httpx.post(
                _config.auth_url,
                data=data,
                timeout=_config.api_timeout,
            )
            response.raise_for_status()
            response_data = response.json()
            auth_token = response_data.get("token")

        except Exception:
            _logger.exception("Mpesa Payment Failed during authentication")
            for payment in payments:
                payment.updated_at = datetime.utcnow()
                payment.status = MsgStatusEnum.rjct
                payment.error_code = SingleDisburseStatusEnum.rjct_payment_failed
                payment.error_msg = "Mpesa Payment Failed during authentication"
            session.commit()
            return

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
                    _logger.exception("Mpesa Payment Failed couldnot get FA from ID")
            else:
                payee_acc_no = payment.to_fa
            headers = {
                "Authorization": f"Bearer {auth_token}",
            }
            data = {
                "amount": int(float(payment.amount)),
                "accountNo": self.get_account_no_from_payee_fa(payee_acc_no),
                "customerType": _config.customer_type,
            }
            try:
                response = httpx.post(
                    _config.payment_url,
                    headers=headers,
                    data=data,
                    timeout=_config.api_timeout,
                )
                _logger.info("MPesa Payment Transfer response: %s", response.content)
                response.raise_for_status()

                # TODO: Do proper Status check here.
                payment.updated_at = datetime.utcnow()
                payment.status = MsgStatusEnum.succ
            except Exception:
                _logger.exception("Mpesa Payment Failed with unknown reason")
                payment.updated_at = datetime.utcnow()
                payment.status = MsgStatusEnum.rjct
                payment.error_code = SingleDisburseStatusEnum.rjct_payment_failed
                payment.error_msg = "Mpesa Payment Failed with unknown reason"

            session.commit()

    def get_account_no_from_payee_fa(self, fa: str) -> str:
        return fa[fa.find(":") + 1 : fa.rfind(".")]


from gctb_translate_id_fa.app import Initializer as TranslateIdInitializer
from openg2p_common_g2pconnect_id_mapper.app import (
    Initializer as G2pConnectMapperInitializer,
)
from openg2p_fastapi_common.app import Initializer
from openg2p_fastapi_common.ping import PingController


class PingController(PingController):
    async def get_ping(self):
        res = await super().get_ping()
        payment_backend = SimpleMpesaPaymentBackendService.get_component()
        if not payment_backend.disburse_loop_thread.is_alive():
            raise BaseAppException("GCTB-MLP-700", "Disbursement Loop is not running")
        return res


class PaymentBackendInitializer(Initializer):
    def initialize(self, **kwargs):
        super().initialize(**kwargs)
        self.payment_backend = SimpleMpesaPaymentBackendService()
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
