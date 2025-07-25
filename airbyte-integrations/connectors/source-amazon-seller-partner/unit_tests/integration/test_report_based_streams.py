#
# Copyright (c) 2024 Airbyte, Inc., all rights reserved.
#


import gzip
import json
from enum import Enum
from http import HTTPStatus
from typing import List, Optional

import freezegun
import pendulum
import pytest
import requests_mock

from airbyte_cdk.models import AirbyteStateMessage, FailureType, Level, SyncMode
from airbyte_cdk.test.entrypoint_wrapper import EntrypointOutput
from airbyte_cdk.test.mock_http import HttpMocker, HttpResponse
from airbyte_cdk.test.mock_http.matcher import HttpRequestMatcher

from .config import CONFIG_END_DATE, CONFIG_START_DATE, MARKETPLACE_ID, NOW, ConfigBuilder
from .request_builder import RequestBuilder
from .response_builder import build_response, response_with_status
from .utils import assert_message_in_log_output, config, find_template, get_stream_by_name, mock_auth, read_output


_DOCUMENT_DOWNLOAD_URL = "https://test.com/download"
_REPORT_ID = "6789087632"
_REPORT_DOCUMENT_ID = "report_document_id"

DEFAULT_EXPECTED_NUMBER_OF_RECORDS = 2  # every test file in resource/http/response contains 2 records
STREAMS = (
    ("GET_FLAT_FILE_ACTIONABLE_ORDER_DATA_SHIPPING", "csv"),
    ("GET_ORDER_REPORT_DATA_SHIPPING", "xml"),
    ("GET_AMAZON_FULFILLED_SHIPMENTS_DATA_GENERAL", "csv"),
    ("GET_FBA_FULFILLMENT_REMOVAL_ORDER_DETAIL_DATA", "csv"),
    ("GET_FBA_FULFILLMENT_REMOVAL_SHIPMENT_DETAIL_DATA", "csv"),
    ("GET_SELLER_FEEDBACK_DATA", "csv"),
    ("GET_FBA_FULFILLMENT_CUSTOMER_SHIPMENT_REPLACEMENT_DATA", "csv"),
    ("GET_LEDGER_DETAIL_VIEW_DATA", "csv"),
    ("GET_AFN_INVENTORY_DATA_BY_COUNTRY", "csv"),
    ("GET_FLAT_FILE_RETURNS_DATA_BY_RETURN_DATE", "csv"),
    ("GET_FBA_FULFILLMENT_CUSTOMER_RETURNS_DATA", "csv"),
    ("GET_FBA_SNS_FORECAST_DATA", "csv"),
    ("GET_AFN_INVENTORY_DATA", "csv"),
    ("GET_MERCHANT_CANCELLED_LISTINGS_DATA", "csv"),
    ("GET_FBA_FULFILLMENT_CUSTOMER_SHIPMENT_PROMOTION_DATA", "csv"),
    ("GET_LEDGER_SUMMARY_VIEW_DATA", "csv"),
    ("GET_FLAT_FILE_ARCHIVED_ORDERS_DATA_BY_ORDER_DATE", "csv"),
    ("GET_FBA_SNS_PERFORMANCE_DATA", "csv"),
    ("GET_FBA_ESTIMATED_FBA_FEES_TXT_DATA", "csv"),
    ("GET_FBA_INVENTORY_PLANNING_DATA", "csv"),
    ("GET_FBA_STORAGE_FEE_CHARGES_DATA", "csv"),
    ("GET_FBA_MYI_UNSUPPRESSED_INVENTORY_DATA", "csv"),
    ("GET_STRANDED_INVENTORY_UI_DATA", "csv"),
    ("GET_FBA_REIMBURSEMENTS_DATA", "csv"),
)


class ReportProcessingStatus(str, Enum):
    CANCELLED = "CANCELLED"
    DONE = "DONE"
    FATAL = "FATAL"
    IN_PROGRESS = "IN_PROGRESS"
    IN_QUEUE = "IN_QUEUE"


def _create_report_request(report_name: str) -> RequestBuilder:
    """
    A POST request needed to start generating a report on Amazon SP platform.
    Performed in ReportsAmazonSPStream._create_report method.
    """
    return RequestBuilder.create_report_endpoint(report_name)


def _check_report_status_request(report_id: str) -> RequestBuilder:
    """
    A GET request needed to check the report generating status.
    Performed in ReportsAmazonSPStream._retrieve_report method.
    """

    return RequestBuilder.check_report_status_endpoint(report_id)


def _get_document_download_url_request(document_id: str) -> RequestBuilder:
    """
    A GET request which returns a URL for the report download.
    """

    return RequestBuilder.get_document_download_url_endpoint(document_id)


def _download_document_request(url: str) -> RequestBuilder:
    """
    A GET request which actually downloads the report.
    Performed in ReportsAmazonSPStream.download_and_decompress_report_document method.
    """

    return RequestBuilder.download_document_endpoint(url)


def _create_report_response(report_id: str, status_code: Optional[HTTPStatus] = HTTPStatus.ACCEPTED) -> HttpResponse:
    response_body = {"reportId": report_id}
    return build_response(response_body, status_code=status_code)


def _check_report_status_response(
    report_name: str,
    processing_status: Optional[ReportProcessingStatus] = ReportProcessingStatus.DONE,
    report_document_id: Optional[str] = None,
) -> HttpResponse:
    if processing_status == ReportProcessingStatus.DONE and not report_document_id:
        raise ValueError("report_document_id value should be passed when processing_status is 'DONE'.")

    response_body = {
        "reportType": report_name,
        "processingStatus": processing_status,
        "marketplaceIds": [MARKETPLACE_ID],
        "reportId": _REPORT_ID,
        "dataEndTime": CONFIG_END_DATE,
        "createdTime": CONFIG_START_DATE,
        "dataStartTime": CONFIG_START_DATE,
        "reportDocumentId": report_document_id,
    }
    if processing_status == ReportProcessingStatus.DONE:
        response_body.update(
            {
                "processingEndTime": CONFIG_START_DATE,
                "processingStartTime": CONFIG_START_DATE,
            }
        )

    return build_response(response_body, status_code=HTTPStatus.OK)


def _get_document_download_url_response(
    document_download_url: str, report_document_id: str, compressed: Optional[bool] = False
) -> HttpResponse:
    response_body = {"reportDocumentId": report_document_id, "url": document_download_url}
    if compressed:
        # See https://developer-docs.amazon.com/sp-api/docs/reports-api-v2021-06-30-reference#compressionalgorithm
        response_body["compressionAlgorithm"] = "GZIP"
    return build_response(response_body, status_code=HTTPStatus.OK)


def _download_document_response(stream_name: str, data_format: Optional[str] = "csv", compressed: Optional[bool] = False) -> HttpResponse:
    response_body = find_template(stream_name, __file__, data_format)
    if compressed:
        response_body = gzip.compress(response_body.encode("iso-8859-1"))
    return HttpResponse(body=response_body, status_code=HTTPStatus.OK)


@freezegun.freeze_time(NOW.isoformat())
class TestFullRefresh:
    @staticmethod
    def _read(stream_name: str, config_: ConfigBuilder, expecting_exception: bool = False) -> EntrypointOutput:
        return read_output(
            config_builder=config_,
            stream_name=stream_name,
            sync_mode=SyncMode.full_refresh,
            expecting_exception=expecting_exception,
        )

    @pytest.mark.parametrize(("stream_name", "data_format"), STREAMS)
    @HttpMocker()
    def test_given_report_when_read_then_return_records(self, stream_name: str, data_format: str, http_mocker: HttpMocker) -> None:
        http_mocker.clear_all_matchers()
        mock_auth(http_mocker)
        http_mocker.post(_create_report_request(stream_name).build(), _create_report_response(_REPORT_ID))
        http_mocker.get(
            _check_report_status_request(_REPORT_ID).build(),
            _check_report_status_response(stream_name, report_document_id=_REPORT_DOCUMENT_ID),
        )
        http_mocker.get(
            _get_document_download_url_request(_REPORT_DOCUMENT_ID).build(),
            _get_document_download_url_response(_DOCUMENT_DOWNLOAD_URL, _REPORT_DOCUMENT_ID),
        )
        http_mocker.get(
            _download_document_request(_DOCUMENT_DOWNLOAD_URL).build(),
            _download_document_response(stream_name, data_format=data_format),
        )

        output = self._read(stream_name, config())
        assert len(output.records) == DEFAULT_EXPECTED_NUMBER_OF_RECORDS

    @pytest.mark.parametrize(("stream_name", "data_format"), STREAMS)
    @HttpMocker()
    def test_given_compressed_report_when_read_then_return_records(
        self, stream_name: str, data_format: str, http_mocker: HttpMocker
    ) -> None:
        http_mocker.clear_all_matchers()
        mock_auth(http_mocker)
        http_mocker.post(_create_report_request(stream_name).build(), _create_report_response(_REPORT_ID))
        http_mocker.get(
            _check_report_status_request(_REPORT_ID).build(),
            _check_report_status_response(stream_name, report_document_id=_REPORT_DOCUMENT_ID),
        )
        http_mocker.get(
            _get_document_download_url_request(_REPORT_DOCUMENT_ID).build(),
            _get_document_download_url_response(_DOCUMENT_DOWNLOAD_URL, _REPORT_DOCUMENT_ID, compressed=True),
        )

        # a workaround to pass compressed document to the mocked response
        document_request = _download_document_request(_DOCUMENT_DOWNLOAD_URL).build()
        document_response = _download_document_response(stream_name, data_format=data_format, compressed=True)
        document_request_matcher = HttpRequestMatcher(document_request, minimum_number_of_expected_match=1)
        # http_mocker._matchers.append(document_request_matcher)

        http_mocker._mocker.get(
            requests_mock.ANY,
            additional_matcher=http_mocker._matches_wrapper(document_request_matcher),
            response_list=[{"content": document_response.body, "status_code": document_response.status_code}],
        )

        output = self._read(stream_name, config())
        assert len(output.records) == DEFAULT_EXPECTED_NUMBER_OF_RECORDS

    @pytest.mark.parametrize(("stream_name", "data_format"), STREAMS)
    @HttpMocker()
    def test_given_http_status_500_then_200_when_create_report_then_retry_and_return_records(
        self, stream_name: str, data_format: str, http_mocker: HttpMocker
    ) -> None:
        http_mocker.clear_all_matchers()
        mock_auth(http_mocker)
        http_mocker.post(
            _create_report_request(stream_name).build(),
            [response_with_status(status_code=HTTPStatus.INTERNAL_SERVER_ERROR), _create_report_response(_REPORT_ID)],
        )
        http_mocker.get(
            _check_report_status_request(_REPORT_ID).build(),
            _check_report_status_response(stream_name, report_document_id=_REPORT_DOCUMENT_ID),
        )
        http_mocker.get(
            _get_document_download_url_request(_REPORT_DOCUMENT_ID).build(),
            _get_document_download_url_response(_DOCUMENT_DOWNLOAD_URL, _REPORT_DOCUMENT_ID),
        )
        http_mocker.get(
            _download_document_request(_DOCUMENT_DOWNLOAD_URL).build(),
            _download_document_response(stream_name, data_format=data_format),
        )

        output = self._read(stream_name, config())
        assert len(output.records) == DEFAULT_EXPECTED_NUMBER_OF_RECORDS

    @pytest.mark.parametrize(("stream_name", "data_format"), STREAMS)
    @HttpMocker()
    def test_given_http_status_500_then_200_when_retrieve_report_then_retry_and_return_records(
        self, stream_name: str, data_format: str, http_mocker: HttpMocker
    ) -> None:
        http_mocker.clear_all_matchers()
        mock_auth(http_mocker)
        http_mocker.post(_create_report_request(stream_name).build(), _create_report_response(_REPORT_ID))
        http_mocker.get(
            _check_report_status_request(_REPORT_ID).build(),
            [
                response_with_status(status_code=HTTPStatus.INTERNAL_SERVER_ERROR),
                _check_report_status_response(stream_name, report_document_id=_REPORT_DOCUMENT_ID),
            ],
        )
        http_mocker.get(
            _get_document_download_url_request(_REPORT_DOCUMENT_ID).build(),
            _get_document_download_url_response(_DOCUMENT_DOWNLOAD_URL, _REPORT_DOCUMENT_ID),
        )
        http_mocker.get(
            _download_document_request(_DOCUMENT_DOWNLOAD_URL).build(),
            _download_document_response(stream_name, data_format=data_format),
        )

        output = self._read(stream_name, config())
        assert len(output.records) == DEFAULT_EXPECTED_NUMBER_OF_RECORDS

    @pytest.mark.parametrize(("stream_name", "data_format"), STREAMS)
    @HttpMocker()
    def test_given_http_status_500_then_200_when_get_document_url_then_retry_and_return_records(
        self, stream_name: str, data_format: str, http_mocker: HttpMocker
    ) -> None:
        http_mocker.clear_all_matchers()
        mock_auth(http_mocker)
        http_mocker.post(_create_report_request(stream_name).build(), _create_report_response(_REPORT_ID))
        http_mocker.get(
            _check_report_status_request(_REPORT_ID).build(),
            _check_report_status_response(stream_name, report_document_id=_REPORT_DOCUMENT_ID),
        )
        http_mocker.get(
            _get_document_download_url_request(_REPORT_DOCUMENT_ID).build(),
            [
                response_with_status(status_code=HTTPStatus.INTERNAL_SERVER_ERROR),
                _get_document_download_url_response(_DOCUMENT_DOWNLOAD_URL, _REPORT_DOCUMENT_ID),
            ],
        )
        http_mocker.get(
            _download_document_request(_DOCUMENT_DOWNLOAD_URL).build(),
            _download_document_response(stream_name, data_format=data_format),
        )

        output = self._read(stream_name, config())
        assert len(output.records) == DEFAULT_EXPECTED_NUMBER_OF_RECORDS

    @pytest.mark.parametrize(("stream_name", "data_format"), STREAMS)
    @HttpMocker()
    def test_given_http_status_500_then_200_when_download_document_then_retry_and_return_records(
        self, stream_name: str, data_format: str, http_mocker: HttpMocker
    ) -> None:
        http_mocker.clear_all_matchers()
        mock_auth(http_mocker)
        http_mocker.post(_create_report_request(stream_name).build(), _create_report_response(_REPORT_ID))
        http_mocker.get(
            _check_report_status_request(_REPORT_ID).build(),
            _check_report_status_response(stream_name, report_document_id=_REPORT_DOCUMENT_ID),
        )
        http_mocker.get(
            _get_document_download_url_request(_REPORT_DOCUMENT_ID).build(),
            _get_document_download_url_response(_DOCUMENT_DOWNLOAD_URL, _REPORT_DOCUMENT_ID),
        )
        http_mocker.get(
            _download_document_request(_DOCUMENT_DOWNLOAD_URL).build(),
            [
                response_with_status(status_code=HTTPStatus.INTERNAL_SERVER_ERROR),
                _download_document_response(stream_name, data_format=data_format),
            ],
        )

        output = self._read(stream_name, config())
        assert len(output.records) == DEFAULT_EXPECTED_NUMBER_OF_RECORDS

    @pytest.mark.parametrize(("stream_name", "data_format"), STREAMS)
    @HttpMocker()
    def test_given_report_access_forbidden_when_read_then_no_records_and_error_logged(
        self, stream_name: str, data_format: str, http_mocker: HttpMocker
    ) -> None:
        http_mocker.clear_all_matchers()
        mock_auth(http_mocker)

        http_mocker.post(_create_report_request(stream_name).build(), response_with_status(status_code=HTTPStatus.FORBIDDEN))

        output = self._read(stream_name, config())
        message_on_access_forbidden = "Forbidden. You don't have permission to access this resource."
        assert output.errors[0].trace.error.failure_type == FailureType.config_error
        assert message_on_access_forbidden in output.errors[0].trace.error.message

    @pytest.mark.parametrize(("stream_name", "data_format"), STREAMS)
    @HttpMocker()
    def test_given_report_status_cancelled_when_read_then_stream_completed_successfully_and_warn_about_cancellation(
        self, stream_name: str, data_format: str, http_mocker: HttpMocker
    ) -> None:
        http_mocker.clear_all_matchers()
        mock_auth(http_mocker)

        http_mocker.post(_create_report_request(stream_name).build(), _create_report_response(_REPORT_ID))
        http_mocker.get(
            _check_report_status_request(_REPORT_ID).build(),
            _check_report_status_response(stream_name, processing_status=ReportProcessingStatus.CANCELLED),
        )

        message_on_report_cancelled = f"Exception while syncing stream {stream_name}"

        output = self._read(stream_name, config())
        assert_message_in_log_output(message=message_on_report_cancelled, entrypoint_output=output, log_level=Level.ERROR)
        assert len(output.records) == 0

    @pytest.mark.parametrize(("stream_name", "data_format"), STREAMS)
    @HttpMocker()
    def test_given_report_status_fatal_when_read_then_exception_raised(
        self, stream_name: str, data_format: str, http_mocker: HttpMocker
    ) -> None:
        http_mocker.clear_all_matchers()
        mock_auth(http_mocker)

        http_mocker.post(_create_report_request(stream_name).build(), _create_report_response(_REPORT_ID))
        http_mocker.get(
            _check_report_status_request(_REPORT_ID).build(),
            _check_report_status_response(
                stream_name, processing_status=ReportProcessingStatus.FATAL, report_document_id=_REPORT_DOCUMENT_ID
            ),
        )

        output = self._read(stream_name, config(), expecting_exception=True)
        assert output.errors[-1].trace.error.failure_type == FailureType.config_error
        config_end_date = CONFIG_END_DATE
        assert (
            f"At least one job could not be completed for slice {{\\'start_time\\': \\'{CONFIG_START_DATE}\\', \\'end_time\\': \\'{config_end_date}\\'}}"
        ) in output.errors[-1].trace.error.message

    @pytest.mark.parametrize(
        ("stream_name", "date_field", "expected_date_value"),
        (
            ("GET_SELLER_FEEDBACK_DATA", "date", "2023-10-20"),
            ("GET_LEDGER_DETAIL_VIEW_DATA", "Date", "2023-11-21"),
            ("GET_LEDGER_SUMMARY_VIEW_DATA", "Date", "2023-12-22"),
        ),
    )
    @HttpMocker()
    def test_given_report_with_incorrect_date_format_when_read_then_formatted(
        self, stream_name: str, date_field: str, expected_date_value: str, http_mocker: HttpMocker
    ) -> None:
        http_mocker.clear_all_matchers()
        mock_auth(http_mocker)

        http_mocker.post(_create_report_request(stream_name).build(), _create_report_response(_REPORT_ID))
        http_mocker.get(
            _check_report_status_request(_REPORT_ID).build(),
            _check_report_status_response(stream_name, report_document_id=_REPORT_DOCUMENT_ID),
        )
        http_mocker.get(
            _get_document_download_url_request(_REPORT_DOCUMENT_ID).build(),
            _get_document_download_url_response(_DOCUMENT_DOWNLOAD_URL, _REPORT_DOCUMENT_ID),
        )
        http_mocker.get(_download_document_request(_DOCUMENT_DOWNLOAD_URL).build(), _download_document_response(stream_name))

        output = self._read(stream_name, config())
        assert len(output.records) == DEFAULT_EXPECTED_NUMBER_OF_RECORDS
        assert output.records[0].record.data.get(date_field) == expected_date_value

    @pytest.mark.parametrize(("stream_name", "data_format"), STREAMS)
    @HttpMocker()
    def test_given_http_error_500_on_create_report_when_read_then_no_records_and_error_logged(
        self, stream_name: str, data_format: str, http_mocker: HttpMocker
    ) -> None:
        http_mocker.clear_all_matchers()
        mock_auth(http_mocker)

        http_mocker.post(
            _create_report_request(stream_name).build(),
            response_with_status(status_code=HTTPStatus.INTERNAL_SERVER_ERROR),
        )

        message_on_backoff_exception = "Giving up _send(...) after 6 tries"

        output = self._read(stream_name, config())

        assert output.errors[0].trace.error.failure_type == FailureType.config_error
        assert_message_in_log_output(message=message_on_backoff_exception, entrypoint_output=output, log_level=Level.ERROR)

    @pytest.mark.parametrize(("stream_name", "data_format"), STREAMS)
    @HttpMocker()
    def test_given_http_error_not_support_account_id_of_type_vendor_when_read_then_no_records_and_error_logged(
        self, stream_name: str, data_format: str, http_mocker: HttpMocker
    ):
        http_mocker.clear_all_matchers()
        mock_auth(http_mocker)
        response_body = {
            "errors": [
                {
                    "code": "InvalidInput",
                    "message": "Report type 301 does not support account ID of type class com.amazon.partner.account.id.VendorGroupId.",
                    "details": "",
                }
            ]
        }
        http_mocker.post(
            _create_report_request(stream_name).build(),
            response_with_status(status_code=HTTPStatus.BAD_REQUEST, body=response_body),
        )

        warning_message = (
            "'POST' request to 'https://sellingpartnerapi-na.amazon.com/reports/2021-06-30/reports' failed with status code '400' and"
            " error message: 'Report type 301 does not support account ID of type class com.amazon.partner.account.id.VendorGroupId.'."
        )

        output = self._read(stream_name, config())

        assert output.errors[0].trace.error.failure_type == FailureType.config_error
        assert_message_in_log_output(message=warning_message, entrypoint_output=output, log_level=Level.ERROR)


@freezegun.freeze_time(NOW.isoformat())
class TestIncremental:
    default_cursor_field = "dataEndTime"

    @staticmethod
    def _read(
        stream_name: str,
        config_: ConfigBuilder,
        state: Optional[List[AirbyteStateMessage]] = None,
        expecting_exception: bool = False,
    ) -> EntrypointOutput:
        return read_output(
            config_builder=config_,
            stream_name=stream_name,
            sync_mode=SyncMode.incremental,
            state=state,
            expecting_exception=expecting_exception,
        )

    @pytest.mark.parametrize(("stream_name", "data_format"), STREAMS)
    @HttpMocker()
    def test_given_report_when_read_then_default_cursor_field_added_to_every_record(
        self, stream_name: str, data_format: str, http_mocker: HttpMocker
    ) -> None:
        http_mocker.clear_all_matchers()
        mock_auth(http_mocker)

        http_mocker.post(_create_report_request(stream_name).build(), _create_report_response(_REPORT_ID))
        http_mocker.get(
            _check_report_status_request(_REPORT_ID).build(),
            _check_report_status_response(stream_name, report_document_id=_REPORT_DOCUMENT_ID),
        )
        http_mocker.get(
            _get_document_download_url_request(_REPORT_DOCUMENT_ID).build(),
            _get_document_download_url_response(_DOCUMENT_DOWNLOAD_URL, _REPORT_DOCUMENT_ID),
        )
        http_mocker.get(
            _download_document_request(_DOCUMENT_DOWNLOAD_URL).build(),
            _download_document_response(stream_name, data_format=data_format),
        )

        output = self._read(stream_name, config())
        assert all([self.default_cursor_field in record.record.data for record in output.records])

    @pytest.mark.parametrize(("stream_name", "data_format"), STREAMS)
    @HttpMocker()
    def test_given_report_when_read_then_state_message_produced_and_state_match_latest_record(
        self, stream_name: str, data_format: str, http_mocker: HttpMocker
    ) -> None:
        """
        This test requires datetime from the records to be higher than the start date of the stream else the cursor value will be the start date
        """
        _config = config()
        http_mocker.clear_all_matchers()
        mock_auth(http_mocker)

        http_mocker.post(_create_report_request(stream_name).build(), _create_report_response(_REPORT_ID))
        http_mocker.get(
            _check_report_status_request(_REPORT_ID).build(),
            _check_report_status_response(stream_name, report_document_id=_REPORT_DOCUMENT_ID),
        )
        http_mocker.get(
            _get_document_download_url_request(_REPORT_DOCUMENT_ID).build(),
            _get_document_download_url_response(_DOCUMENT_DOWNLOAD_URL, _REPORT_DOCUMENT_ID),
        )
        http_mocker.get(
            _download_document_request(_DOCUMENT_DOWNLOAD_URL).build(),
            _download_document_response(stream_name, data_format=data_format),
        )

        output = self._read(stream_name, _config)
        assert (
            len(output.state_messages) == 2
        )  # we have two messages here, one for the slice and once of the "ensure_at_least_one_state_emitted"

        cursor_field = get_stream_by_name(stream_name, _config.build()).cursor_field
        cursor_value_from_latest_record = output.records[-1].record.data.get(cursor_field)

        most_recent_state = output.most_recent_state.stream_state
        # format between record and cursor value can differ hence we rely on pendulum parsing to ignore those discrepancies
        assert pendulum.parse(most_recent_state.__dict__[cursor_field]) == pendulum.parse(cursor_value_from_latest_record)


@freezegun.freeze_time(NOW.isoformat())
class TestVendorSalesReportsFullRefresh:
    data_format = "json"
    selling_program = ("RETAIL", "FRESH")

    @staticmethod
    def _read(stream_name: str, config_: ConfigBuilder, expecting_exception: bool = False) -> EntrypointOutput:
        return read_output(
            config_builder=config_,
            stream_name=stream_name,
            sync_mode=SyncMode.full_refresh,
            expecting_exception=expecting_exception,
        )

    @staticmethod
    def _get_stream_name(selling_program: str) -> str:
        return f"GET_VENDOR_FORECASTING_{selling_program}_REPORT"

    @staticmethod
    def _get_report_request_body(selling_program: str) -> str:
        return json.dumps(
            {
                "reportType": "GET_VENDOR_FORECASTING_REPORT",
                "marketplaceIds": [MARKETPLACE_ID],
                "reportOptions": {"sellingProgram": selling_program},
            }
        )

    @pytest.mark.parametrize("selling_program", selling_program)
    @HttpMocker()
    def test_given_report_when_read_then_return_records(self, selling_program: str, http_mocker: HttpMocker) -> None:
        http_mocker.clear_all_matchers()
        mock_auth(http_mocker)
        stream_name = self._get_stream_name(selling_program)
        create_report_request_body = self._get_report_request_body(selling_program)
        http_mocker.post(
            _create_report_request(stream_name).with_body(create_report_request_body).build(),
            _create_report_response(_REPORT_ID),
        )
        http_mocker.get(
            _check_report_status_request(_REPORT_ID).build(),
            _check_report_status_response(stream_name, report_document_id=_REPORT_DOCUMENT_ID),
        )
        http_mocker.get(
            _get_document_download_url_request(_REPORT_DOCUMENT_ID).build(),
            _get_document_download_url_response(_DOCUMENT_DOWNLOAD_URL, _REPORT_DOCUMENT_ID),
        )

        http_mocker.get(
            _download_document_request(_DOCUMENT_DOWNLOAD_URL).build(),
            _download_document_response(stream_name, data_format=self.data_format),
        )

        output = self._read(stream_name, config())
        assert len(output.records) == DEFAULT_EXPECTED_NUMBER_OF_RECORDS

    @pytest.mark.parametrize("selling_program", selling_program)
    @HttpMocker()
    def test_given_compressed_report_when_read_then_return_records(self, selling_program: str, http_mocker: HttpMocker) -> None:
        http_mocker.clear_all_matchers()
        mock_auth(http_mocker)
        stream_name = self._get_stream_name(selling_program)
        create_report_request_body = self._get_report_request_body(selling_program)
        http_mocker.post(
            _create_report_request(stream_name).with_body(create_report_request_body).build(),
            _create_report_response(_REPORT_ID),
        )
        http_mocker.get(
            _check_report_status_request(_REPORT_ID).build(),
            _check_report_status_response(stream_name, report_document_id=_REPORT_DOCUMENT_ID),
        )
        http_mocker.get(
            _get_document_download_url_request(_REPORT_DOCUMENT_ID).build(),
            _get_document_download_url_response(_DOCUMENT_DOWNLOAD_URL, _REPORT_DOCUMENT_ID, compressed=True),
        )

        # a workaround to pass compressed document to the mocked response
        document_request = _download_document_request(_DOCUMENT_DOWNLOAD_URL).build()
        document_response = _download_document_response(stream_name, data_format=self.data_format, compressed=True)
        document_request_matcher = HttpRequestMatcher(document_request, minimum_number_of_expected_match=1)
        # http_mocker._matchers.append(document_request_matcher)

        http_mocker._mocker.get(
            requests_mock.ANY,
            additional_matcher=http_mocker._matches_wrapper(document_request_matcher),
            response_list=[{"content": document_response.body, "status_code": document_response.status_code}],
        )

        output = self._read(stream_name, config())
        assert len(output.records) == DEFAULT_EXPECTED_NUMBER_OF_RECORDS

    @pytest.mark.parametrize("selling_program", selling_program)
    @HttpMocker()
    def test_given_http_status_500_then_200_when_create_report_then_retry_and_return_records(
        self, selling_program: str, http_mocker: HttpMocker
    ) -> None:
        http_mocker.clear_all_matchers()
        mock_auth(http_mocker)
        stream_name = self._get_stream_name(selling_program)
        create_report_request_body = self._get_report_request_body(selling_program)
        http_mocker.post(
            _create_report_request(stream_name).with_body(create_report_request_body).build(),
            [response_with_status(status_code=HTTPStatus.INTERNAL_SERVER_ERROR), _create_report_response(_REPORT_ID)],
        )
        http_mocker.get(
            _check_report_status_request(_REPORT_ID).build(),
            _check_report_status_response(stream_name, report_document_id=_REPORT_DOCUMENT_ID),
        )
        http_mocker.get(
            _get_document_download_url_request(_REPORT_DOCUMENT_ID).build(),
            _get_document_download_url_response(_DOCUMENT_DOWNLOAD_URL, _REPORT_DOCUMENT_ID),
        )
        http_mocker.get(
            _download_document_request(_DOCUMENT_DOWNLOAD_URL).build(),
            _download_document_response(stream_name, data_format=self.data_format),
        )

        output = self._read(stream_name, config())
        assert len(output.records) == DEFAULT_EXPECTED_NUMBER_OF_RECORDS

    @pytest.mark.parametrize("selling_program", selling_program)
    @HttpMocker()
    def test_given_http_status_500_then_200_when_retrieve_report_then_retry_and_return_records(
        self, selling_program: str, http_mocker: HttpMocker
    ) -> None:
        http_mocker.clear_all_matchers()
        mock_auth(http_mocker)
        stream_name = self._get_stream_name(selling_program)
        create_report_request_body = self._get_report_request_body(selling_program)
        http_mocker.post(
            _create_report_request(stream_name).with_body(create_report_request_body).build(),
            _create_report_response(_REPORT_ID),
        )
        http_mocker.get(
            _check_report_status_request(_REPORT_ID).build(),
            [
                response_with_status(status_code=HTTPStatus.INTERNAL_SERVER_ERROR),
                _check_report_status_response(stream_name, report_document_id=_REPORT_DOCUMENT_ID),
            ],
        )
        http_mocker.get(
            _get_document_download_url_request(_REPORT_DOCUMENT_ID).build(),
            _get_document_download_url_response(_DOCUMENT_DOWNLOAD_URL, _REPORT_DOCUMENT_ID),
        )
        http_mocker.get(
            _download_document_request(_DOCUMENT_DOWNLOAD_URL).build(),
            _download_document_response(stream_name, data_format=self.data_format),
        )

        output = self._read(stream_name, config())
        assert len(output.records) == DEFAULT_EXPECTED_NUMBER_OF_RECORDS

    @pytest.mark.parametrize("selling_program", selling_program)
    @HttpMocker()
    def test_given_http_status_500_then_200_when_get_document_url_then_retry_and_return_records(
        self, selling_program: str, http_mocker: HttpMocker
    ) -> None:
        http_mocker.clear_all_matchers()
        mock_auth(http_mocker)
        stream_name = self._get_stream_name(selling_program)
        create_report_request_body = self._get_report_request_body(selling_program)
        http_mocker.post(
            _create_report_request(stream_name).with_body(create_report_request_body).build(),
            _create_report_response(_REPORT_ID),
        )
        http_mocker.get(
            _check_report_status_request(_REPORT_ID).build(),
            _check_report_status_response(stream_name, report_document_id=_REPORT_DOCUMENT_ID),
        )
        http_mocker.get(
            _get_document_download_url_request(_REPORT_DOCUMENT_ID).build(),
            [
                response_with_status(status_code=HTTPStatus.INTERNAL_SERVER_ERROR),
                _get_document_download_url_response(_DOCUMENT_DOWNLOAD_URL, _REPORT_DOCUMENT_ID),
            ],
        )
        http_mocker.get(
            _download_document_request(_DOCUMENT_DOWNLOAD_URL).build(),
            _download_document_response(stream_name, data_format=self.data_format),
        )

        output = self._read(stream_name, config())
        assert len(output.records) == DEFAULT_EXPECTED_NUMBER_OF_RECORDS

    @pytest.mark.parametrize("selling_program", selling_program)
    @HttpMocker()
    def test_given_http_status_500_then_200_when_download_document_then_retry_and_return_records(
        self, selling_program: str, http_mocker: HttpMocker
    ) -> None:
        http_mocker.clear_all_matchers()
        mock_auth(http_mocker)
        stream_name = self._get_stream_name(selling_program)
        create_report_request_body = self._get_report_request_body(selling_program)
        http_mocker.post(
            _create_report_request(stream_name).with_body(create_report_request_body).build(),
            _create_report_response(_REPORT_ID),
        )
        http_mocker.get(
            _check_report_status_request(_REPORT_ID).build(),
            _check_report_status_response(stream_name, report_document_id=_REPORT_DOCUMENT_ID),
        )
        http_mocker.get(
            _get_document_download_url_request(_REPORT_DOCUMENT_ID).build(),
            _get_document_download_url_response(_DOCUMENT_DOWNLOAD_URL, _REPORT_DOCUMENT_ID),
        )
        http_mocker.get(
            _download_document_request(_DOCUMENT_DOWNLOAD_URL).build(),
            [
                response_with_status(status_code=HTTPStatus.INTERNAL_SERVER_ERROR),
                _download_document_response(stream_name, data_format=self.data_format),
            ],
        )

        output = self._read(stream_name, config())
        assert len(output.records) == DEFAULT_EXPECTED_NUMBER_OF_RECORDS

    @pytest.mark.parametrize("selling_program", selling_program)
    @HttpMocker()
    def test_given_report_access_forbidden_when_read_then_no_records_and_error_logged(
        self, selling_program: str, http_mocker: HttpMocker
    ) -> None:
        http_mocker.clear_all_matchers()
        mock_auth(http_mocker)
        stream_name = self._get_stream_name(selling_program)
        create_report_request_body = self._get_report_request_body(selling_program)
        http_mocker.post(
            _create_report_request(stream_name).with_body(create_report_request_body).build(),
            response_with_status(status_code=HTTPStatus.FORBIDDEN),
        )

        output = self._read(stream_name, config())
        message_on_access_forbidden = "Forbidden. You don't have permission to access this resource."
        assert output.errors[0].trace.error.failure_type == FailureType.config_error
        assert message_on_access_forbidden in output.errors[0].trace.error.message

    @pytest.mark.parametrize("selling_program", selling_program)
    @HttpMocker()
    def test_given_report_status_cancelled_when_read_then_stream_completed_successfully_and_warn_about_cancellation(
        self, selling_program: str, http_mocker: HttpMocker
    ) -> None:
        http_mocker.clear_all_matchers()
        mock_auth(http_mocker)
        stream_name = self._get_stream_name(selling_program)
        create_report_request_body = self._get_report_request_body(selling_program)
        http_mocker.post(
            _create_report_request(stream_name).with_body(create_report_request_body).build(),
            _create_report_response(_REPORT_ID),
        )
        http_mocker.get(
            _check_report_status_request(_REPORT_ID).build(),
            _check_report_status_response(stream_name, processing_status=ReportProcessingStatus.CANCELLED),
        )

        message_on_report_cancelled = f"Exception while syncing stream {stream_name}"

        output = self._read(stream_name, config())
        assert_message_in_log_output(message=message_on_report_cancelled, entrypoint_output=output, log_level=Level.ERROR)
        assert len(output.records) == 0

    @pytest.mark.parametrize("selling_program", selling_program)
    @HttpMocker()
    def test_given_report_status_fatal_when_read_then_exception_raised(self, selling_program: str, http_mocker: HttpMocker) -> None:
        http_mocker.clear_all_matchers()
        mock_auth(http_mocker)
        stream_name = self._get_stream_name(selling_program)
        create_report_request_body = self._get_report_request_body(selling_program)
        http_mocker.post(
            _create_report_request(stream_name).with_body(create_report_request_body).build(),
            _create_report_response(_REPORT_ID),
        )
        http_mocker.get(
            _check_report_status_request(_REPORT_ID).build(),
            _check_report_status_response(
                stream_name, processing_status=ReportProcessingStatus.FATAL, report_document_id=_REPORT_DOCUMENT_ID
            ),
        )

        output = self._read(stream_name, config(), expecting_exception=True)
        assert output.errors[-1].trace.error.failure_type == FailureType.config_error
        assert "At least one job could not be completed for slice {}" in output.errors[-1].trace.error.message

    @pytest.mark.parametrize("selling_program", selling_program)
    @HttpMocker()
    def test_given_http_error_500_on_create_report_when_read_then_no_records_and_error_logged(
        self, selling_program: str, http_mocker: HttpMocker
    ) -> None:
        http_mocker.clear_all_matchers()
        mock_auth(http_mocker)
        stream_name = self._get_stream_name(selling_program)
        create_report_request_body = self._get_report_request_body(selling_program)
        http_mocker.post(
            _create_report_request(stream_name).with_body(create_report_request_body).build(),
            response_with_status(status_code=HTTPStatus.INTERNAL_SERVER_ERROR),
        )

        message_on_backoff_exception = "Giving up _send(...) after 6 tries"

        output = self._read(stream_name, config())

        assert output.errors[0].trace.error.failure_type == FailureType.config_error
        assert_message_in_log_output(message=message_on_backoff_exception, entrypoint_output=output, log_level=Level.ERROR)
