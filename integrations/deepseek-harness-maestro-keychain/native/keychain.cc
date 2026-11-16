#include <node_api.h>

#ifdef __APPLE__
#include <CoreFoundation/CoreFoundation.h>
#include <Security/Security.h>
#endif

#include <cstdio>
#include <string>
#include <vector>

namespace {

void ThrowTypeError(napi_env env, const char* message) {
  napi_throw_type_error(env, nullptr, message);
}

#ifdef __APPLE__

void ThrowStatus(napi_env env, const char* operation, OSStatus status) {
  char code[64];
  char message[160];
  std::snprintf(code, sizeof(code), "MAESTRO_KEYCHAIN_%ld", static_cast<long>(status));
  std::snprintf(
      message,
      sizeof(message),
      "maestro-keychain: Security.framework %s failed with OSStatus %ld",
      operation,
      static_cast<long>(status));
  napi_throw_error(env, code, message);
}

CFStringRef ReadString(napi_env env, napi_value value, const char* field) {
  napi_valuetype type;
  if (napi_typeof(env, value, &type) != napi_ok || type != napi_string) {
    char message[96];
    std::snprintf(message, sizeof(message), "maestro-keychain: %s must be a string", field);
    ThrowTypeError(env, message);
    return nullptr;
  }
  size_t size = 0;
  if (napi_get_value_string_utf8(env, value, nullptr, 0, &size) != napi_ok) {
    ThrowTypeError(env, "maestro-keychain: could not measure a string argument");
    return nullptr;
  }
  std::vector<char> bytes(size + 1);
  size_t copied = 0;
  if (napi_get_value_string_utf8(env, value, bytes.data(), bytes.size(), &copied) != napi_ok) {
    ThrowTypeError(env, "maestro-keychain: could not read a string argument");
    return nullptr;
  }
  return CFStringCreateWithBytes(
      kCFAllocatorDefault,
      reinterpret_cast<const UInt8*>(bytes.data()),
      static_cast<CFIndex>(copied),
      kCFStringEncodingUTF8,
      false);
}

CFMutableDictionaryRef NewBaseQuery(CFStringRef service, CFStringRef account) {
  CFMutableDictionaryRef query = CFDictionaryCreateMutable(
      kCFAllocatorDefault,
      0,
      &kCFTypeDictionaryKeyCallBacks,
      &kCFTypeDictionaryValueCallBacks);
  CFDictionarySetValue(query, kSecClass, kSecClassGenericPassword);
  CFDictionarySetValue(query, kSecAttrService, service);
  if (account != nullptr) CFDictionarySetValue(query, kSecAttrAccount, account);
  return query;
}

bool ReadServiceAccount(
    napi_env env,
    napi_callback_info info,
    napi_value* argv,
    size_t expected,
    CFStringRef* service,
    CFStringRef* account) {
  size_t argc = expected;
  if (napi_get_cb_info(env, info, &argc, argv, nullptr, nullptr) != napi_ok || argc != expected) {
    ThrowTypeError(env, "maestro-keychain: wrong number of arguments");
    return false;
  }
  *service = ReadString(env, argv[0], "service");
  if (*service == nullptr) return false;
  if (account != nullptr) {
    *account = ReadString(env, argv[1], "account");
    if (*account == nullptr) {
      CFRelease(*service);
      return false;
    }
  }
  return true;
}

napi_value Get(napi_env env, napi_callback_info info) {
  napi_value argv[2];
  CFStringRef service = nullptr;
  CFStringRef account = nullptr;
  if (!ReadServiceAccount(env, info, argv, 2, &service, &account)) return nullptr;

  CFMutableDictionaryRef query = NewBaseQuery(service, account);
  CFDictionarySetValue(query, kSecReturnData, kCFBooleanTrue);
  CFDictionarySetValue(query, kSecMatchLimit, kSecMatchLimitOne);
  CFTypeRef result = nullptr;
  const OSStatus status = SecItemCopyMatching(query, &result);
  CFRelease(query);
  CFRelease(account);
  CFRelease(service);

  if (status == errSecItemNotFound) {
    napi_value value;
    napi_get_null(env, &value);
    return value;
  }
  if (status != errSecSuccess) {
    if (result != nullptr) CFRelease(result);
    ThrowStatus(env, "read", status);
    return nullptr;
  }
  if (result == nullptr || CFGetTypeID(result) != CFDataGetTypeID()) {
    if (result != nullptr) CFRelease(result);
    napi_throw_error(env, "MAESTRO_KEYCHAIN_INVALID_RESULT", "maestro-keychain: Security.framework returned non-data");
    return nullptr;
  }

  CFDataRef data = static_cast<CFDataRef>(result);
  napi_value value;
  const napi_status napi_status_value = napi_create_buffer_copy(
      env,
      static_cast<size_t>(CFDataGetLength(data)),
      CFDataGetBytePtr(data),
      nullptr,
      &value);
  CFRelease(result);
  if (napi_status_value != napi_ok) {
    napi_throw_error(env, "MAESTRO_KEYCHAIN_NAPI", "maestro-keychain: could not allocate result buffer");
    return nullptr;
  }
  return value;
}

napi_value Set(napi_env env, napi_callback_info info) {
  napi_value argv[3];
  CFStringRef service = nullptr;
  CFStringRef account = nullptr;
  if (!ReadServiceAccount(env, info, argv, 3, &service, &account)) return nullptr;

  bool is_buffer = false;
  if (napi_is_buffer(env, argv[2], &is_buffer) != napi_ok || !is_buffer) {
    CFRelease(account);
    CFRelease(service);
    ThrowTypeError(env, "maestro-keychain: value must be a Buffer");
    return nullptr;
  }
  void* bytes = nullptr;
  size_t size = 0;
  if (napi_get_buffer_info(env, argv[2], &bytes, &size) != napi_ok) {
    CFRelease(account);
    CFRelease(service);
    ThrowTypeError(env, "maestro-keychain: could not read value Buffer");
    return nullptr;
  }

  CFDataRef data = CFDataCreate(
      kCFAllocatorDefault,
      static_cast<const UInt8*>(bytes),
      static_cast<CFIndex>(size));
  CFMutableDictionaryRef query = NewBaseQuery(service, account);
  CFMutableDictionaryRef attributes = CFDictionaryCreateMutable(
      kCFAllocatorDefault,
      0,
      &kCFTypeDictionaryKeyCallBacks,
      &kCFTypeDictionaryValueCallBacks);
  CFDictionarySetValue(attributes, kSecValueData, data);

  OSStatus status = SecItemUpdate(query, attributes);
  if (status == errSecItemNotFound) {
    CFMutableDictionaryRef item = CFDictionaryCreateMutableCopy(kCFAllocatorDefault, 0, query);
    CFDictionarySetValue(item, kSecValueData, data);
    status = SecItemAdd(item, nullptr);
    CFRelease(item);
    // A concurrent creator may win between update and add. Retrying the
    // update gives this operation ordinary last-writer-wins semantics.
    if (status == errSecDuplicateItem) status = SecItemUpdate(query, attributes);
  }

  CFRelease(attributes);
  CFRelease(query);
  CFRelease(data);
  CFRelease(account);
  CFRelease(service);
  if (status != errSecSuccess) {
    ThrowStatus(env, "write", status);
    return nullptr;
  }
  napi_value value;
  napi_get_undefined(env, &value);
  return value;
}

napi_value Delete(napi_env env, napi_callback_info info) {
  napi_value argv[2];
  CFStringRef service = nullptr;
  CFStringRef account = nullptr;
  if (!ReadServiceAccount(env, info, argv, 2, &service, &account)) return nullptr;
  CFMutableDictionaryRef query = NewBaseQuery(service, account);
  const OSStatus status = SecItemDelete(query);
  CFRelease(query);
  CFRelease(account);
  CFRelease(service);
  if (status != errSecSuccess && status != errSecItemNotFound) {
    ThrowStatus(env, "delete", status);
    return nullptr;
  }
  napi_value value;
  napi_get_boolean(env, status == errSecSuccess, &value);
  return value;
}

bool AppendAccount(napi_env env, napi_value array, uint32_t index, CFTypeRef item) {
  if (item == nullptr || CFGetTypeID(item) != CFDictionaryGetTypeID()) {
    napi_throw_error(env, "MAESTRO_KEYCHAIN_INVALID_RESULT", "maestro-keychain: Security.framework returned non-attributes");
    return false;
  }
  CFTypeRef raw = CFDictionaryGetValue(static_cast<CFDictionaryRef>(item), kSecAttrAccount);
  if (raw == nullptr || CFGetTypeID(raw) != CFStringGetTypeID()) {
    napi_throw_error(env, "MAESTRO_KEYCHAIN_INVALID_RESULT", "maestro-keychain: Keychain item has no string account");
    return false;
  }
  CFStringRef account = static_cast<CFStringRef>(raw);
  const CFIndex length = CFStringGetLength(account);
  const CFIndex capacity = CFStringGetMaximumSizeForEncoding(length, kCFStringEncodingUTF8) + 1;
  std::vector<char> bytes(static_cast<size_t>(capacity));
  if (!CFStringGetCString(account, bytes.data(), capacity, kCFStringEncodingUTF8)) {
    napi_throw_error(env, "MAESTRO_KEYCHAIN_INVALID_RESULT", "maestro-keychain: Keychain account is not UTF-8");
    return false;
  }
  napi_value value;
  if (napi_create_string_utf8(env, bytes.data(), NAPI_AUTO_LENGTH, &value) != napi_ok
      || napi_set_element(env, array, index, value) != napi_ok) {
    napi_throw_error(env, "MAESTRO_KEYCHAIN_NAPI", "maestro-keychain: could not create account result");
    return false;
  }
  return true;
}

napi_value ListAccounts(napi_env env, napi_callback_info info) {
  napi_value argv[1];
  CFStringRef service = nullptr;
  if (!ReadServiceAccount(env, info, argv, 1, &service, nullptr)) return nullptr;
  CFMutableDictionaryRef query = NewBaseQuery(service, nullptr);
  CFDictionarySetValue(query, kSecReturnAttributes, kCFBooleanTrue);
  CFDictionarySetValue(query, kSecMatchLimit, kSecMatchLimitAll);
  CFTypeRef result = nullptr;
  const OSStatus status = SecItemCopyMatching(query, &result);
  CFRelease(query);
  CFRelease(service);

  napi_value array;
  napi_create_array(env, &array);
  if (status == errSecItemNotFound) return array;
  if (status != errSecSuccess) {
    if (result != nullptr) CFRelease(result);
    ThrowStatus(env, "list", status);
    return nullptr;
  }

  bool ok = true;
  if (result != nullptr && CFGetTypeID(result) == CFArrayGetTypeID()) {
    CFArrayRef items = static_cast<CFArrayRef>(result);
    const CFIndex count = CFArrayGetCount(items);
    for (CFIndex index = 0; index < count && ok; ++index) {
      ok = AppendAccount(env, array, static_cast<uint32_t>(index), CFArrayGetValueAtIndex(items, index));
    }
  } else {
    ok = AppendAccount(env, array, 0, result);
  }
  if (result != nullptr) CFRelease(result);
  return ok ? array : nullptr;
}

#else

napi_value Unsupported(napi_env env, napi_callback_info) {
  napi_throw_error(
      env,
      "MAESTRO_KEYCHAIN_UNSUPPORTED",
      "maestro-keychain: Security.framework is available only on macOS");
  return nullptr;
}

#endif

napi_value Init(napi_env env, napi_value exports) {
#ifdef __APPLE__
  napi_property_descriptor properties[] = {
      {"get", nullptr, Get, nullptr, nullptr, nullptr, napi_default_jsproperty, nullptr},
      {"set", nullptr, Set, nullptr, nullptr, nullptr, napi_default_jsproperty, nullptr},
      {"delete", nullptr, Delete, nullptr, nullptr, nullptr, napi_default_jsproperty, nullptr},
      {"listAccounts", nullptr, ListAccounts, nullptr, nullptr, nullptr, napi_default_jsproperty, nullptr},
  };
#else
  napi_property_descriptor properties[] = {
      {"get", nullptr, Unsupported, nullptr, nullptr, nullptr, napi_default_jsproperty, nullptr},
      {"set", nullptr, Unsupported, nullptr, nullptr, nullptr, napi_default_jsproperty, nullptr},
      {"delete", nullptr, Unsupported, nullptr, nullptr, nullptr, napi_default_jsproperty, nullptr},
      {"listAccounts", nullptr, Unsupported, nullptr, nullptr, nullptr, napi_default_jsproperty, nullptr},
  };
#endif
  if (napi_define_properties(env, exports, 4, properties) != napi_ok) return nullptr;
  return exports;
}

}  // namespace

NAPI_MODULE(NODE_GYP_MODULE_NAME, Init)
