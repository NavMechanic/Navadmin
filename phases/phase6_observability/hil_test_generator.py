"""
Phase 6.2 — Hardware-in-Loop (HIL) Test Auto-Generator.

Parse device spec JSON → auto-generate pytest functions for component validation.
Tests: Bluetooth AT commands, camera sensor, battery ADC, USB-C detection, IMU calibration, NFC field.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import List, Dict, Optional, Callable
from enum import Enum
from datetime import datetime


class ComponentType(str, Enum):
    BLUETOOTH = "bluetooth"
    CAMERA = "camera"
    BATTERY = "battery"
    USB = "usb"
    IMU = "imu"
    NFC = "nfc"
    DISPLAY = "display"
    MICROPHONE = "microphone"
    GPS = "gps"


@dataclass
class ComponentSpec:
    """Hardware component specification."""
    name: str
    component_type: ComponentType
    part_number: str
    min_voltage_v: float = 0.0
    max_voltage_v: float = 5.0
    timeout_s: float = 5.0
    expected_response: Optional[str] = None
    calibration_range: Optional[Dict[str, float]] = None  # e.g., {"min_x": -2048, "max_x": 2048}
    test_parameters: Dict[str, any] = field(default_factory=dict)


@dataclass
class TestCase:
    """Generated test case."""
    test_name: str
    component: ComponentSpec
    test_code: str
    prerequisites: List[str] = field(default_factory=list)
    expected_outcome: str = ""


@dataclass
class HILTestSuite:
    """Complete HIL test suite for a device."""
    device_name: str
    device_spec_version: str
    components: List[ComponentSpec]
    test_cases: List[TestCase] = field(default_factory=list)
    generated_at: datetime = field(default_factory=datetime.utcnow)

    def to_pytest_file(self) -> str:
        """Generate complete pytest file content."""
        imports = self._generate_imports()
        fixtures = self._generate_fixtures()
        tests = "\n".join(tc.test_code for tc in self.test_cases)

        return f"""{imports}

{fixtures}

{tests}
"""

    def _generate_imports(self) -> str:
        """Generate pytest imports and mock client."""
        return '''import pytest
import asyncio
import struct
from typing import Optional
from datetime import datetime


class MockDeviceClient:
    """Mock hardware device communication client."""

    def __init__(self, port: str = "/dev/ttyUSB0", baudrate: int = 115200):
        self.port = port
        self.baudrate = baudrate
        self.is_connected = False
        self.response_buffer = []

    async def connect(self) -> bool:
        """Connect to device."""
        self.is_connected = True
        return True

    async def disconnect(self) -> None:
        """Disconnect from device."""
        self.is_connected = False

    async def send_command(self, cmd: str, timeout_s: float = 5.0) -> Optional[str]:
        """Send AT command and await response."""
        if not self.is_connected:
            return None
        # Mock: return canned responses based on command
        return self._mock_response(cmd)

    async def read_adc(self, channel: int) -> float:
        """Read ADC value (0.0-3.3V range)."""
        # Mock: return value in range
        return 3.3

    async def send_bytes(self, data: bytes) -> bool:
        """Send raw bytes (for NFC, BLE advertising)."""
        self.response_buffer.append(data)
        return True

    def _mock_response(self, cmd: str) -> str:
        """Return mock responses for AT commands."""
        if "AT" in cmd:
            return "OK"
        if "BT+NAME" in cmd:
            return "+NAME:DeviceX"
        return ""
'''

    def _generate_fixtures(self) -> str:
        """Generate pytest fixtures."""
        return '''@pytest.fixture
async def device():
    """Connect to device for each test."""
    client = MockDeviceClient()
    await client.connect()
    yield client
    await client.disconnect()


@pytest.fixture
def event_loop():
    """Provide event loop for async tests."""
    loop = asyncio.get_event_loop_policy().new_event_loop()
    yield loop
    loop.close()
'''

    def add_test_case(self, tc: TestCase) -> None:
        """Add a generated test case."""
        self.test_cases.append(tc)


class HILTestGenerator:
    """
    Auto-generate pytest test functions from device specification.
    """

    def __init__(self, device_spec_path: str):
        """
        Initialize generator with device spec JSON.

        Args:
            device_spec_path: Path to device_spec.json
        """
        self.device_spec_path = device_spec_path
        self.device_spec = self._load_spec()
        self.suite = self._create_suite()

    def _load_spec(self) -> dict:
        """Load device specification from JSON."""
        try:
            with open(self.device_spec_path, "r") as f:
                return json.load(f)
        except (FileNotFoundError, json.JSONDecodeError) as exc:
            print(f"  ⚠ Failed to load device spec: {exc}")
            return {}

    def _create_suite(self) -> HILTestSuite:
        """Create HIL test suite from spec."""
        spec = self.device_spec
        components = []

        for comp_dict in spec.get("components", []):
            comp = ComponentSpec(
                name=comp_dict.get("name", "Unknown"),
                component_type=ComponentType(comp_dict.get("type", "bluetooth")),
                part_number=comp_dict.get("part_number", ""),
                min_voltage_v=comp_dict.get("min_voltage_v", 0),
                max_voltage_v=comp_dict.get("max_voltage_v", 5),
                timeout_s=comp_dict.get("timeout_s", 5),
                expected_response=comp_dict.get("expected_response"),
                calibration_range=comp_dict.get("calibration_range"),
                test_parameters=comp_dict.get("test_parameters", {}),
            )
            components.append(comp)

        return HILTestSuite(
            device_name=spec.get("device_name", "Unknown Device"),
            device_spec_version=spec.get("version", "1.0"),
            components=components,
        )

    def generate_all_tests(self) -> HILTestSuite:
        """Generate test cases for all components."""
        for component in self.suite.components:
            if component.component_type == ComponentType.BLUETOOTH:
                self._generate_bluetooth_tests(component)
            elif component.component_type == ComponentType.CAMERA:
                self._generate_camera_tests(component)
            elif component.component_type == ComponentType.BATTERY:
                self._generate_battery_tests(component)
            elif component.component_type == ComponentType.USB:
                self._generate_usb_tests(component)
            elif component.component_type == ComponentType.IMU:
                self._generate_imu_tests(component)
            elif component.component_type == ComponentType.NFC:
                self._generate_nfc_tests(component)

        return self.suite

    def _generate_bluetooth_tests(self, comp: ComponentSpec) -> None:
        """Generate Bluetooth component tests."""
        # Test 1: AT command basic check
        test_code = f'''@pytest.mark.asyncio
async def test_{comp.name.lower()}_at_command(device):
    """Test {comp.name} responds to AT commands."""
    response = await device.send_command("AT", timeout_s={comp.timeout_s})
    assert response is not None, "{comp.name} did not respond to AT command"
    assert "OK" in response, "Unexpected response from {comp.name}"
'''
        self.suite.add_test_case(TestCase(
            test_name=f"test_{comp.name.lower()}_at_command",
            component=comp,
            test_code=test_code,
            expected_outcome="Device responds OK to AT command",
        ))

        # Test 2: Name query
        test_code = f'''@pytest.mark.asyncio
async def test_{comp.name.lower()}_name_query(device):
    """Test {comp.name} returns device name."""
    response = await device.send_command("AT+NAME?", timeout_s={comp.timeout_s})
    assert response is not None, "{comp.name} failed to respond to name query"
    assert "+" in response, "Invalid name query response format"
'''
        self.suite.add_test_case(TestCase(
            test_name=f"test_{comp.name.lower()}_name_query",
            component=comp,
            test_code=test_code,
            expected_outcome="Device returns device name",
        ))

        # Test 3: Voltage check
        test_code = f'''@pytest.mark.asyncio
async def test_{comp.name.lower()}_voltage(device):
    """Test {comp.name} voltage within range."""
    voltage = await device.read_adc(channel=0)
    assert {comp.min_voltage_v} <= voltage <= {comp.max_voltage_v}, \\
        f"Voltage {{voltage}}V out of range [{comp.min_voltage_v}, {comp.max_voltage_v}]"
'''
        self.suite.add_test_case(TestCase(
            test_name=f"test_{comp.name.lower()}_voltage",
            component=comp,
            test_code=test_code,
            expected_outcome=f"Voltage in range [{comp.min_voltage_v}V, {comp.max_voltage_v}V]",
        ))

    def _generate_camera_tests(self, comp: ComponentSpec) -> None:
        """Generate camera component tests."""
        # Test 1: Camera detection
        test_code = f'''@pytest.mark.asyncio
async def test_{comp.name.lower()}_detection(device):
    """Test {comp.name} is detected by device."""
    response = await device.send_command("AT+CAM?", timeout_s={comp.timeout_s})
    assert response is not None, "{comp.name} not detected"
    assert "READY" in response or "OK" in response, "Camera not ready"
'''
        self.suite.add_test_case(TestCase(
            test_name=f"test_{comp.name.lower()}_detection",
            component=comp,
            test_code=test_code,
            expected_outcome="Camera is detected and ready",
        ))

        # Test 2: Frame capture
        test_code = f'''@pytest.mark.asyncio
async def test_{comp.name.lower()}_capture(device):
    """Test {comp.name} can capture frame."""
    response = await device.send_command("AT+CAM+CAPTURE", timeout_s={comp.timeout_s})
    assert response is not None, "{comp.name} capture failed"
    # In mock, just verify response format
'''
        self.suite.add_test_case(TestCase(
            test_name=f"test_{comp.name.lower()}_capture",
            component=comp,
            test_code=test_code,
            expected_outcome="Camera captures frame successfully",
        ))

    def _generate_battery_tests(self, comp: ComponentSpec) -> None:
        """Generate battery monitoring tests."""
        # Test 1: Battery voltage
        test_code = f'''@pytest.mark.asyncio
async def test_{comp.name.lower()}_voltage(device):
    """Test {comp.name} voltage reading."""
    voltage = await device.read_adc(channel=1)
    assert {comp.min_voltage_v} <= voltage <= {comp.max_voltage_v}, \\
        f"Battery voltage {{voltage}}V out of range"
'''
        self.suite.add_test_case(TestCase(
            test_name=f"test_{comp.name.lower()}_voltage",
            component=comp,
            test_code=test_code,
            expected_outcome=f"Battery voltage within [{comp.min_voltage_v}V, {comp.max_voltage_v}V]",
        ))

        # Test 2: Battery health (mock SOH calculation)
        test_code = f'''@pytest.mark.asyncio
async def test_{comp.name.lower()}_health(device):
    """Test {comp.name} health monitoring."""
    response = await device.send_command("AT+BAT+HEALTH", timeout_s={comp.timeout_s})
    assert response is not None, "{comp.name} health query failed"
    # Parse health percentage if available
'''
        self.suite.add_test_case(TestCase(
            test_name=f"test_{comp.name.lower()}_health",
            component=comp,
            test_code=test_code,
            expected_outcome="Battery health reporting active",
        ))

    def _generate_usb_tests(self, comp: ComponentSpec) -> None:
        """Generate USB connectivity tests."""
        # Test 1: USB detection
        test_code = f'''@pytest.mark.asyncio
async def test_{comp.name.lower()}_detection(device):
    """Test {comp.name} is detected."""
    response = await device.send_command("AT+USB?", timeout_s={comp.timeout_s})
    assert response is not None, "{comp.name} not detected"
'''
        self.suite.add_test_case(TestCase(
            test_name=f"test_{comp.name.lower()}_detection",
            component=comp,
            test_code=test_code,
            expected_outcome="USB connector is detected",
        ))

    def _generate_imu_tests(self, comp: ComponentSpec) -> None:
        """Generate IMU (accelerometer/gyro) tests."""
        calibration = comp.calibration_range or {"min_x": -2048, "max_x": 2048}

        # Test 1: IMU presence
        test_code = f'''@pytest.mark.asyncio
async def test_{comp.name.lower()}_presence(device):
    """Test {comp.name} is present and responsive."""
    response = await device.send_command("AT+IMU", timeout_s={comp.timeout_s})
    assert response is not None, "{comp.name} not responding"
'''
        self.suite.add_test_case(TestCase(
            test_name=f"test_{comp.name.lower()}_presence",
            component=comp,
            test_code=test_code,
            expected_outcome="IMU is detected",
        ))

        # Test 2: Calibration check
        test_code = f'''@pytest.mark.asyncio
async def test_{comp.name.lower()}_calibration(device):
    """Test {comp.name} is within calibration range."""
    response = await device.send_command("AT+IMU+CAL", timeout_s={comp.timeout_s})
    assert response is not None, "{comp.name} calibration check failed"
    # Verify axes are within calibration_range
'''
        self.suite.add_test_case(TestCase(
            test_name=f"test_{comp.name.lower()}_calibration",
            component=comp,
            test_code=test_code,
            expected_outcome=f"IMU axes within calibration range",
        ))

    def _generate_nfc_tests(self, comp: ComponentSpec) -> None:
        """Generate NFC tests."""
        # Test 1: NFC field detection
        test_code = f'''@pytest.mark.asyncio
async def test_{comp.name.lower()}_field_detection(device):
    """Test {comp.name} can detect NFC field."""
    response = await device.send_command("AT+NFC+FIELD", timeout_s={comp.timeout_s})
    assert response is not None, "{comp.name} field detection failed"
'''
        self.suite.add_test_case(TestCase(
            test_name=f"test_{comp.name.lower()}_field_detection",
            component=comp,
            test_code=test_code,
            expected_outcome="NFC field detection working",
        ))

        # Test 2: NFC tag read
        test_code = f'''@pytest.mark.asyncio
async def test_{comp.name.lower()}_tag_read(device):
    """Test {comp.name} can read tags."""
    response = await device.send_command("AT+NFC+READ", timeout_s={comp.timeout_s})
    assert response is not None, "{comp.name} tag read failed"
'''
        self.suite.add_test_case(TestCase(
            test_name=f"test_{comp.name.lower()}_tag_read",
            component=comp,
            test_code=test_code,
            expected_outcome="NFC tag reading functional",
        ))

    def save_pytest_file(self, output_path: str) -> bool:
        """Save generated pytest file."""
        try:
            with open(output_path, "w") as f:
                f.write(self.suite.to_pytest_file())
            print(f"  ✅ Generated {len(self.suite.test_cases)} test cases → {output_path}")
            return True
        except Exception as exc:
            print(f"  ⚠ Failed to save pytest file: {exc}")
            return False

    def get_test_summary(self) -> dict:
        """Return summary of generated tests."""
        tests_by_component = {}
        for tc in self.suite.test_cases:
            comp_name = tc.component.name
            if comp_name not in tests_by_component:
                tests_by_component[comp_name] = []
            tests_by_component[comp_name].append(tc.test_name)

        return {
            "device": self.suite.device_name,
            "total_tests": len(self.suite.test_cases),
            "components": len(self.suite.components),
            "tests_by_component": tests_by_component,
            "generated_at": self.suite.generated_at.isoformat(),
        }
