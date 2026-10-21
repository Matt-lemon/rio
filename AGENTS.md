# 프로젝트 작업 규칙

## Google Home API 필수 준수 기준

사용자가 2026-10-09 지정한 다음 공식 문서는 Google Home API 관련 작업의 **필수 준수 기준**이다.

- https://developers.home.google.com/apis/android/release-notes?hl=ko
- 작업 전 [적용 규칙](docs/google-home-api-compliance-rules.md)을 읽고, 사용하는 버전에 맞는 최신 공식 요구사항을 확인한다.
- 구현, 데이터 생성, 검증 및 적용 판단에서 해당 버전의 필수 변경사항·최소 버전·권한 제한을 충족해야 한다. 충족 여부가 확인되지 않으면 준수 완료로 보고하지 않는다.
- 과거 출시의 요구사항과 최신 출시의 요구사항을 혼합하지 않는다. Google Home Android API 규칙을 SmartThings API의 스키마로 간주하지 않는다.
- 사용자의 작업 대기 및 검증 보류 지시는 유지한다. 문서를 확인하는 것으로 기기 제어, 검증 재개 또는 운영 변경이 승인되는 것은 아니다.
