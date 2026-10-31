import unittest
from ui_errors import response_error


class ErrorTests(unittest.TestCase):
    def test_all_errors_have_safe_actionable_details_and_tracking_id(self):
        cases=[(401,'ابتدا وارد Atlas شوید.','login_required'),(401,'رمز ورود صحیح نیست.','login_failed'),
            (403,'مبدأ درخواست معتبر نیست.','origin_mismatch'),(403,'درخواست منقضی شد؛ صفحه را تازه کنید.','csrf_expired'),
            (421,'آدرس میزبان مجاز نیست.','host_not_allowed'),(429,'تلاش زیاد؛ پنج دقیقه دیگر امتحان کنید.','rate_limited'),
            (400,'پورت ورودی باید آزاد باشد.','port_unavailable'),(400,'آدرس SOCKS معتبر نیست.','proxy_invalid'),
            (400,'یک اینباند فعال انتخاب کنید.','inbound_required'),(400,'ابتدا عملیات نیمه‌تمام را بررسی کنید.','operation_pending'),
            (400,'Host هم‌زمان تغییر کرده.','stale_revision'),(400,'ثبت Host تأیید نشد؛ عملیات نگه داشته شد.','write_unconfirmed'),
            (400,'لایسنس معتبر نیست.','license_required'),(503,'همگام‌سازی پنل نیاز به بررسی دارد.','metadata_stale'),
            (500,'عملیات کامل نشد.','internal_error')]
        for status,message,code in cases:
            with self.subTest(code=code):
                body=response_error(message,status,'/api/hosts/create')
                self.assertEqual(body['error_code'],code)
                self.assertEqual(body['error'],message)
                self.assertTrue(body['error_title'] and body['error_hint'])
                self.assertEqual(len(body['error_id']),12)

    def test_host_recovery_points_to_hosts_and_does_not_encourage_duplicate_writes(self):
        body=response_error('ثبت Host تأیید نشد.',400,'/api/hosts/create')
        self.assertIn('میزبان',body['error_hint'])
        self.assertIn('تکرار نکن',body['error_hint'])

    def test_credentials_and_license_keys_are_not_echoed(self):
        body=response_error('SOCKS socks5://person:private-password@example.org:1080 token=private-token FS-'+'x'*40)
        value=str(body)
        for secret in ['person','private-password','private-token','FS-'+'x'*40]:self.assertNotIn(secret,value)

    def test_timeout_and_certificate_causes_remain_distinct(self):
        self.assertEqual(response_error('پاسخ نرسید',504,code='upstream_timeout')['error_code'],'upstream_timeout')
        self.assertEqual(response_error('گواهی نامعتبر',502,code='tls_error')['error_code'],'tls_error')


if __name__=='__main__':unittest.main()
