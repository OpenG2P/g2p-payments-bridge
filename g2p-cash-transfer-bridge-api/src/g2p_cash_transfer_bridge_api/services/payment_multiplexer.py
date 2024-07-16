import logging
import re
import uuid
import random

from g2p_cash_transfer_bridge_core.models.disburse import (
    DisburseRequest,
    DisburseResponse,
    DisburseTxnStatusRequest,
    DisburseTxnStatusResponse,
    SingleDisburseResponse,
    SingleDisburseTxnStatusResponse,
    TxnStatusAttributeTypeEnum,
)
from g2p_cash_transfer_bridge_core.models.orm.payment_list import PaymentListItem
from g2p_cash_transfer_bridge_core.services.id_translate_service import (
    IdTranslateService,
)
from g2p_cash_transfer_bridge_core.services.payment_multiplexer import (
    PaymentMultiplexerService as CorePaymentMultiplexerService,
)
from openg2p_fastapi_common.errors.http_exceptions import BadRequestError

from ..config import Settings

_config = Settings.get_config()
_logger = logging.getLogger(_config.logging_default_logger_name)


class PaymentMultiplexerService(CorePaymentMultiplexerService):
    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self._id_translate_service = IdTranslateService.get_component()

    @property
    def id_translate_service(self):
        if not self._id_translate_service:
            self._id_translate_service = IdTranslateService.get_component()
        return self._id_translate_service

    async def get_payment_backend_from_fa(self, fa: str):
        for mapping in _config.multiplex_fa_backend_mapping:
            if re.search(mapping.regex, fa):
                return mapping.name
        return None

    async def disburse(self, disburse_request: DisburseRequest):
        error_messages = [
            "Beneficiary Account is Closed",
            "Beneficiary Account is Dormant",
            "Beneficiary Account not found",
            "Beneficiary Account has a No Credit Policy"
        ]
        backends = []
        if _config.get_backend_name_from_translate:
            backends = [f'backend{i}' for i in range(len(disburse_request.disbursements))]

        for i, disbursement in enumerate(disburse_request.disbursements):
            if random.random() < 0.2: # 20% chance of failure
                status = "rjct"
                error_code = "rjct_payment_failed"
                error_msg = random.choice(error_messages)
            else:
                status = "succ"
                error_code = None
                error_msg = None

            backend_name = None
            if _config.get_backend_name_from_translate:
                backend_name = backends[i]

            await PaymentListItem.insert(
                disburse_request.transaction_id, disbursement, backend_name=backend_name, status=status,
                error_code=error_code, error_msg=error_msg
            )

    async def disbursement_status(
        self, status_request: DisburseTxnStatusRequest
    ) -> DisburseTxnStatusResponse:
        ref_ids = status_request.txnstatus_request.attribute_value
        if (
            status_request.txnstatus_request.attribute_type
            == TxnStatusAttributeTypeEnum.reference_id_list
        ):
            if not isinstance(ref_ids, list):
                raise BadRequestError(
                    "GCTB-PMS-350", "attribute_value is supposed to be a list."
                )
            payment_list = await PaymentListItem.get_by_request_ids(ref_ids)
            return DisburseTxnStatusResponse(
                transaction_id=status_request.transaction_id,
                correlation_id=str(uuid.uuid4()),
                txnstatus_response=SingleDisburseTxnStatusResponse(
                    txn_type="disburse",
                    txn_status=[
                        SingleDisburseResponse(
                            reference_id=payment_item.request_id,
                            timestamp=payment_item.updated_at,
                            status=payment_item.status,
                            status_reason_code=payment_item.error_code,
                            status_reason_message=payment_item.error_msg,
                            amount=payment_item.amount,
                            # TODO:
                            # payer_fa = payment_item.from_fa
                            payee_fa=payment_item.to_fa,
                            currency_code=payment_item.currency,
                        )
                        for payment_item in payment_list
                    ],
                ),
            )
        elif (
            status_request.txnstatus_request.attribute_type
            == TxnStatusAttributeTypeEnum.transaction_id
        ):
            # TODO: handle ids not present in db
            txn_id = status_request.txnstatus_request.attribute_value
            if not isinstance(ref_ids, str):
                raise BadRequestError(
                    "GCTB-PMS-350", "attribute_value is supposed to be a string."
                )
            payment_list = await PaymentListItem.get_by_batch_id(txn_id)
            return DisburseTxnStatusResponse(
                transaction_id=status_request.transaction_id,
                correlation_id=str(uuid.uuid4()),
                txnstatus_response=SingleDisburseTxnStatusResponse(
                    txn_type="disburse",
                    txn_status=DisburseResponse(
                        transaction_id=txn_id,
                        disbursements_status=[
                            SingleDisburseResponse(
                                reference_id=payment_item.request_id,
                                timestamp=payment_item.updated_at,
                                status=payment_item.status,
                                status_reason_code=payment_item.error_code,
                                status_reason_message=payment_item.error_msg,
                                amount=payment_item.amount,
                                # TODO:
                                # payer_fa = payment_item.from_fa
                                payee_fa=payment_item.to_fa,
                                currency_code=payment_item.currency,
                            )
                            for payment_item in payment_list
                        ],
                    ),
                ),
            )

        raise NotImplementedError()
