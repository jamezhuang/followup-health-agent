"""Optional recognition boundary: no credentials, audio, or document text in telemetry.

Tencent SDK is loaded only when enabled. Tests inject a prepared request factory;
no fake provider output is used by the application.
"""
import asyncio
import base64
import importlib
import importlib.util
import json
import time
from io import BytesIO
from ..workspace.parsers import ParseError, parse_file


class RecognitionError(Exception):
    def __init__(self, code, message, status=422):
        self.code, self.message, self.status = code, message, status
        super().__init__(message)


class Recognition:
    def __init__(self, settings, repository, request_factory=None):
        self.settings, self.repo = settings, repository
        self.request_factory = request_factory or self._prepare_sdk_request

    def enabled(self, feature):
        return (self.settings.recognition_provider=='tencent' and
                getattr(self.settings,feature+'_enabled') and
                bool(self.settings.tencent_secret_id and self.settings.tencent_secret_key))

    def capabilities(self):
        sdk=importlib.util.find_spec('tencentcloud') is not None
        return {'ocr_ready':self.enabled('ocr') and sdk, 'asr_ready':self.enabled('asr') and sdk,
                'provider':self.settings.recognition_provider,'sdk_installed':sdk,
                'asr_max_seconds':60,'asr_max_base64_bytes':3*1024*1024,'ocr_max_pdf_pages':20}

    def _prepare_sdk_request(self, service, action, payload):
        try:
            from tencentcloud.common.credential import Credential
            from tencentcloud.common.profile.client_profile import ClientProfile
            from tencentcloud.common.profile.http_profile import HttpProfile
            version='v20181119' if service=='ocr' else 'v20190614'
            clients=importlib.import_module(f'tencentcloud.{service}.{version}.{service}_client')
            models=importlib.import_module(f'tencentcloud.{service}.{version}.models')
        except ImportError:
            raise RecognitionError('sdk_unavailable','腾讯云识别SDK尚未安装，请按接入说明安装可选依赖。',503) from None
        profile=ClientProfile();profile.httpProfile=HttpProfile()
        profile.httpProfile.endpoint=f'{service}.tencentcloudapi.com'
        profile.httpProfile.reqTimeout=min(120,max(10,int(self.settings.timeout)))
        client=getattr(clients,'OcrClient' if service=='ocr' else 'AsrClient')(
            Credential(self.settings.tencent_secret_id,self.settings.tencent_secret_key),self.settings.tencent_region,profile)
        request=getattr(models,action+'Request')();request.from_json_string(json.dumps(payload))
        def invoke():
            return json.loads(getattr(client,action)(request).to_json_string())
        return invoke

    def _request(self, service, action, payload, run_id):
        invoke=self.request_factory(service,action,payload)  # local configuration failures are not fake API attempts
        identifier=self.repo.begin_call(run_id,'OCR 识别' if service=='ocr' else '语音识别',
                                        service+'_recognition','tencent:'+action,1)
        started=time.monotonic();status='failed';code='recognition_provider_error'
        try:
            result=invoke();status='ok';code=''
            return result
        except Exception as error:
            provider_code = error.get_code() if callable(getattr(error, 'get_code', None)) else ''
            messages = {
                'AuthFailure': '腾讯云识别鉴权失败，请检查密钥是否正确及有效。',
                'UnauthorizedOperation': '腾讯云识别未获得调用权限，请检查服务是否开通和密钥权限。',
                'ResourceUnavailable': '腾讯云识别服务或额度不可用，请检查服务开通状态、资源包和账户余额。',
                'LimitExceeded': '腾讯云识别调用超过限额，请稍后重试或检查额度。',
                'RequestLimitExceeded': '识别请求过于频繁，请稍后重试。',
                'InvalidParameterValue': '腾讯云无法读取该图片或音频，请检查格式、大小和内容后重试。',
                'FailedOperation': '腾讯云未能完成识别，请检查图片清晰度、文件内容或服务状态。',
            }
            message = messages.get(provider_code.split('.')[0], '腾讯云识别调用失败，请检查服务权限、额度、文件格式或稍后重试。')
            raise RecognitionError('recognition_provider_error', message, 502) from None
        finally:
            self.repo.end_call(identifier,status=status,error_code=code,
                failure_reason='' if status=='ok' else '腾讯云识别调用失败（未记录原始错误或资料）',
                duration_ms=round((time.monotonic()-started)*1000),input_tokens=None,output_tokens=None,
                total_tokens=None,usage_known=False)

    async def parse_document(self, data, suffix, run_id):
        try:
            return await asyncio.to_thread(parse_file,data,suffix)
        except ParseError as error:
            if error.code!='ocr_required' and suffix not in {'.png','.jpg','.jpeg','.bmp','.webp'}:
                raise
        if not self.enabled('ocr'):
            raise RecognitionError('ocr_not_configured','该文件需要OCR；腾讯云OCR尚未启用或未配置凭据。',503)
        if suffix=='.webp':
            from PIL import Image
            try:
                with Image.open(BytesIO(data)) as image:
                    if image.width*image.height>25000000:
                        raise ValueError('image too large')
                    output=BytesIO();image.convert('RGB').save(output,format='PNG');data=output.getvalue()
            except Exception:
                raise RecognitionError('invalid_image','图片无法读取或像素过大，请转换为清晰的JPG/PNG后上传。') from None
        if suffix=='.pdf':
            from pypdf import PdfReader, PdfWriter
            reader=PdfReader(BytesIO(data))
            if len(reader.pages)>20:
                raise RecognitionError('ocr_page_limit','当前OCR单次最多处理20页PDF，请拆分上传。')
            result=[]
            for i,page in enumerate(reader.pages,1):
                text=(page.extract_text() or '').strip()
                if text:
                    result.append({'location':f'第 {i} 页','text':text})
                else:
                    writer=PdfWriter();writer.add_page(page);buffer=BytesIO();writer.write(buffer)
                    text=await self.ocr_page(buffer.getvalue(),True,run_id)
                    result.append({'location':f'第 {i} 页（OCR识别，需核对）','text':text})
            return result
        return [{'location':'图片OCR识别（需核对）','text':await self.ocr_page(data,False,run_id)}]

    async def ocr_page(self, data, is_pdf, run_id):
        encoded=base64.b64encode(data).decode('ascii')
        if len(encoded)>7*1024*1024:
            raise RecognitionError('ocr_size_limit','单页OCR编码后不能超过7MB，请压缩或拆分文件。')
        payload={'ImageBase64':encoded}
        if is_pdf:payload.update(IsPdf=True,PdfPageNumber=1)
        result=await asyncio.to_thread(self._request,'ocr','GeneralAccurateOCR',payload,run_id)
        text='\n'.join(row.get('DetectedText','') for row in result.get('TextDetections',[])).strip()
        if not text:raise RecognitionError('ocr_empty','未识别到文字，请提供清晰的报告。')
        return text

    async def transcribe(self, data, suffix, run_id):
        if not self.enabled('asr'):
            raise RecognitionError('asr_not_configured','腾讯云语音识别尚未启用或未配置凭据。',503)
        formats={'.wav':'wav','.mp3':'mp3','.m4a':'m4a','.aac':'aac','.amr':'amr','.pcm':'pcm','.ogg':'ogg-opus'}
        if suffix not in formats:
            raise RecognitionError('audio_format','暂不支持该音频格式。')
        encoded=base64.b64encode(data).decode('ascii')
        if not data or len(encoded)>3*1024*1024:
            raise RecognitionError('audio_size_limit','短音频编码后不能超过3MB，时长须在60秒内。')
        payload={'EngSerViceType':self.settings.tencent_asr_engine,'SourceType':1,
                 'VoiceFormat':formats[suffix],'Data':encoded,'DataLen':len(data)}
        result=await asyncio.to_thread(self._request,'asr','SentenceRecognition',payload,run_id)
        text=result.get('Result','').strip()
        if not text:raise RecognitionError('asr_empty','没有识别到语音，请重新录制。')
        return {'text':text,'requires_confirmation':True,'audio_duration_ms':result.get('AudioDuration')}
