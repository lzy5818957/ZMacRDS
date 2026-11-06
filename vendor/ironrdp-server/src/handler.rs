use ironrdp_ainput as ainput;
use ironrdp_pdu::input::fast_path::{self, SynchronizeFlags};
use ironrdp_pdu::input::mouse::PointerFlags;
use ironrdp_pdu::input::mouse_rel::PointerRelFlags;
use ironrdp_pdu::input::mouse_x::PointerXFlags;
use ironrdp_pdu::input::sync::SyncToggleFlags;
use ironrdp_pdu::input::{MousePdu, MouseRelPdu, MouseXPdu, scan_code, unicode};

/// Keyboard Event
///
/// Describes a keyboard event received from the client
///
#[derive(Debug)]
pub enum KeyboardEvent {
    Pressed { code: u8, extended: bool },
    Released { code: u8, extended: bool },
    UnicodePressed(u16),
    UnicodeReleased(u16),
    Synchronize(SynchronizeFlags),
}

/// Mouse Event
///
/// Describes a mouse event received from the client
///
#[derive(Debug)]
pub enum MouseEvent {
    Move { x: u16, y: u16 },
    RightPressed,
    RightReleased,
    LeftPressed,
    LeftReleased,
    MiddlePressed,
    MiddleReleased,
    Button4Pressed,
    Button4Released,
    Button5Pressed,
    Button5Released,
    VerticalScroll { value: i16 },
    Scroll { x: i32, y: i32 },
    RelMove { x: i32, y: i32 },
}

/// Input Event Handler for an RDP server
///
/// Whenever the RDP server will receive an input event from a client, the relevant callback from
/// this handler will be called
///
/// # Example
///
/// ```
/// use ironrdp_server::{KeyboardEvent, MouseEvent, RdpServerInputHandler};
///
/// pub struct InputHandler;
///
/// impl RdpServerInputHandler for InputHandler {
///     fn keyboard(&mut self, event: KeyboardEvent) {
///         match event {
///             KeyboardEvent::Pressed { code, .. } => println!("Pressed {}", code),
///             KeyboardEvent::Released { code, .. } => println!("Released {}", code),
///             other => println!("unhandled event: {:?}", other),
///         };
///     }
///
///     fn mouse(&mut self, event: MouseEvent) {
///         let result = match event {
///             MouseEvent::Move { x, y } => println!("Moved mouse to {} {}", x, y),
///             other => println!("unhandled event: {:?}", other),
///         };
///     }
/// }
/// ```
pub trait RdpServerInputHandler: Send {
    fn keyboard(&mut self, event: KeyboardEvent);
    fn mouse(&mut self, event: MouseEvent);
}

impl From<(u8, fast_path::KeyboardFlags)> for KeyboardEvent {
    fn from((key, flags): (u8, fast_path::KeyboardFlags)) -> Self {
        let extended = flags.contains(fast_path::KeyboardFlags::EXTENDED);
        if flags.contains(fast_path::KeyboardFlags::RELEASE) {
            KeyboardEvent::Released { code: key, extended }
        } else {
            KeyboardEvent::Pressed { code: key, extended }
        }
    }
}

impl From<(u16, fast_path::KeyboardFlags)> for KeyboardEvent {
    fn from((key, flags): (u16, fast_path::KeyboardFlags)) -> Self {
        if flags.contains(fast_path::KeyboardFlags::RELEASE) {
            KeyboardEvent::UnicodeReleased(key)
        } else {
            KeyboardEvent::UnicodePressed(key)
        }
    }
}

impl From<(u16, scan_code::KeyboardFlags)> for KeyboardEvent {
    #[expect(
        clippy::as_conversions,
        clippy::cast_possible_truncation,
        reason = "we are truncating the value on purpose"
    )]
    fn from((key, flags): (u16, scan_code::KeyboardFlags)) -> Self {
        let extended = flags.contains(scan_code::KeyboardFlags::EXTENDED);

        if flags.contains(scan_code::KeyboardFlags::RELEASE) {
            KeyboardEvent::Released {
                code: key as u8,
                extended,
            }
        } else {
            KeyboardEvent::Pressed {
                code: key as u8,
                extended,
            }
        }
    }
}

impl From<(u16, unicode::KeyboardFlags)> for KeyboardEvent {
    fn from((key, flags): (u16, unicode::KeyboardFlags)) -> Self {
        if flags.contains(unicode::KeyboardFlags::RELEASE) {
            KeyboardEvent::UnicodeReleased(key)
        } else {
            KeyboardEvent::UnicodePressed(key)
        }
    }
}

impl From<SynchronizeFlags> for KeyboardEvent {
    fn from(value: SynchronizeFlags) -> Self {
        KeyboardEvent::Synchronize(value)
    }
}

impl From<SyncToggleFlags> for KeyboardEvent {
    #[expect(
        clippy::as_conversions,
        clippy::cast_possible_truncation,
        reason = "we are truncating the value on purpose"
    )]
    fn from(value: SyncToggleFlags) -> Self {
        KeyboardEvent::Synchronize(SynchronizeFlags::from_bits_retain(value.bits() as u8))
    }
}

impl From<MousePdu> for MouseEvent {
    fn from(value: MousePdu) -> Self {
        if value.flags.contains(PointerFlags::LEFT_BUTTON) {
            if value.flags.contains(PointerFlags::DOWN) {
                MouseEvent::LeftPressed
            } else {
                MouseEvent::LeftReleased
            }
        } else if value.flags.contains(PointerFlags::RIGHT_BUTTON) {
            if value.flags.contains(PointerFlags::DOWN) {
                MouseEvent::RightPressed
            } else {
                MouseEvent::RightReleased
            }
        } else if value.flags.contains(PointerFlags::VERTICAL_WHEEL) {
            MouseEvent::VerticalScroll {
                value: value.number_of_wheel_rotation_units,
            }
        } else {
            MouseEvent::Move {
                x: value.x_position,
                y: value.y_position,
            }
        }
    }
}

impl From<MouseXPdu> for MouseEvent {
    fn from(value: MouseXPdu) -> Self {
        if value.flags.contains(PointerXFlags::BUTTON1) {
            if value.flags.contains(PointerXFlags::DOWN) {
                MouseEvent::LeftPressed
            } else {
                MouseEvent::LeftReleased
            }
        } else if value.flags.contains(PointerXFlags::BUTTON2) {
            if value.flags.contains(PointerXFlags::DOWN) {
                MouseEvent::RightPressed
            } else {
                MouseEvent::RightReleased
            }
        } else {
            MouseEvent::Move {
                x: value.x_position,
                y: value.y_position,
            }
        }
    }
}

impl From<MouseRelPdu> for MouseEvent {
    fn from(value: MouseRelPdu) -> Self {
        if value.flags.contains(PointerRelFlags::BUTTON1) {
            if value.flags.contains(PointerRelFlags::DOWN) {
                MouseEvent::LeftPressed
            } else {
                MouseEvent::LeftReleased
            }
        } else if value.flags.contains(PointerRelFlags::BUTTON2) {
            if value.flags.contains(PointerRelFlags::DOWN) {
                MouseEvent::RightPressed
            } else {
                MouseEvent::RightReleased
            }
        } else if value.flags.contains(PointerRelFlags::BUTTON3) {
            if value.flags.contains(PointerRelFlags::DOWN) {
                MouseEvent::MiddlePressed
            } else {
                MouseEvent::MiddleReleased
            }
        } else if value.flags.contains(PointerRelFlags::XBUTTON1) {
            if value.flags.contains(PointerRelFlags::DOWN) {
                MouseEvent::Button4Pressed
            } else {
                MouseEvent::Button4Released
            }
        } else if value.flags.contains(PointerRelFlags::XBUTTON2) {
            if value.flags.contains(PointerRelFlags::DOWN) {
                MouseEvent::Button5Pressed
            } else {
                MouseEvent::Button5Released
            }
        } else {
            MouseEvent::RelMove {
                x: value.x_delta.into(),
                y: value.y_delta.into(),
            }
        }
    }
}

impl From<ainput::MousePdu> for MouseEvent {
    fn from(value: ainput::MousePdu) -> Self {
        use ainput::MouseEventFlags;

        if value.flags.contains(MouseEventFlags::BUTTON1) {
            if value.flags.contains(MouseEventFlags::DOWN) {
                MouseEvent::LeftPressed
            } else {
                MouseEvent::LeftReleased
            }
        } else if value.flags.contains(MouseEventFlags::BUTTON2) {
            if value.flags.contains(MouseEventFlags::DOWN) {
                MouseEvent::RightPressed
            } else {
                MouseEvent::RightReleased
            }
        } else if value.flags.contains(MouseEventFlags::BUTTON3) {
            if value.flags.contains(MouseEventFlags::DOWN) {
                MouseEvent::MiddlePressed
            } else {
                MouseEvent::MiddleReleased
            }
        } else if value.flags.contains(MouseEventFlags::WHEEL) {
            MouseEvent::Scroll { x: value.x, y: value.y }
        } else if value.flags.contains(MouseEventFlags::REL) {
            MouseEvent::RelMove { x: value.x, y: value.y }
        } else if value.flags.contains(MouseEventFlags::MOVE) {
            // assume moves are 0 <= u16::MAX
            MouseEvent::Move {
                x: value.x.try_into().unwrap_or(0),
                y: value.y.try_into().unwrap_or(0),
            }
        } else {
            MouseEvent::Move { x: 0, y: 0 }
        }
    }
}

// ZMacRDS local patch: button PDUs carry their own coordinates. See MS-RDPBCGR
// 2.2.8.1.1.3.1.1.3; wheel PDUs explicitly do not carry usable coordinates.
pub(crate) fn dispatch_mouse(handler: &mut dyn RdpServerInputHandler, pdu: MousePdu) {
    if pdu.flags.contains(PointerFlags::VERTICAL_WHEEL) {
        handler.mouse(MouseEvent::VerticalScroll {
            value: pdu.number_of_wheel_rotation_units,
        });
        return;
    }
    if pdu.flags.contains(PointerFlags::HORIZONTAL_WHEEL) {
        handler.mouse(MouseEvent::Scroll {
            x: i32::from(pdu.number_of_wheel_rotation_units),
            y: 0,
        });
        return;
    }
    handler.mouse(MouseEvent::Move {
        x: pdu.x_position,
        y: pdu.y_position,
    });
    let down = pdu.flags.contains(PointerFlags::DOWN);
    for (flag, press, release) in [
        (
            PointerFlags::LEFT_BUTTON,
            MouseEvent::LeftPressed,
            MouseEvent::LeftReleased,
        ),
        (
            PointerFlags::RIGHT_BUTTON,
            MouseEvent::RightPressed,
            MouseEvent::RightReleased,
        ),
        (
            PointerFlags::MIDDLE_BUTTON_OR_WHEEL,
            MouseEvent::MiddlePressed,
            MouseEvent::MiddleReleased,
        ),
    ] {
        if pdu.flags.contains(flag) {
            handler.mouse(if down { press } else { release });
        }
    }
}
pub(crate) fn dispatch_mouse_x(handler: &mut dyn RdpServerInputHandler, pdu: MouseXPdu) {
    handler.mouse(MouseEvent::Move {
        x: pdu.x_position,
        y: pdu.y_position,
    });
    let down = pdu.flags.contains(PointerXFlags::DOWN);
    if pdu.flags.contains(PointerXFlags::BUTTON1) {
        handler.mouse(if down {
            MouseEvent::Button4Pressed
        } else {
            MouseEvent::Button4Released
        });
    }
    if pdu.flags.contains(PointerXFlags::BUTTON2) {
        handler.mouse(if down {
            MouseEvent::Button5Pressed
        } else {
            MouseEvent::Button5Released
        });
    }
}

#[cfg(test)]
mod coordinate_tests {
    use super::*;
    #[derive(Default)]
    struct Recorder(Vec<MouseEvent>);
    impl RdpServerInputHandler for Recorder {
        fn keyboard(&mut self, _: KeyboardEvent) {}
        fn mouse(&mut self, event: MouseEvent) {
            self.0.push(event);
        }
    }
    #[test]
    fn click_without_move_flag_preserves_position_and_button() {
        let mut r = Recorder::default();
        dispatch_mouse(
            &mut r,
            MousePdu {
                flags: PointerFlags::LEFT_BUTTON | PointerFlags::DOWN,
                x_position: 731,
                y_position: 419,
                number_of_wheel_rotation_units: 0,
            },
        );
        assert!(matches!(
            r.0.as_slice(),
            [MouseEvent::Move { x: 731, y: 419 }, MouseEvent::LeftPressed]
        ));
        r.0.clear();
        dispatch_mouse(
            &mut r,
            MousePdu {
                flags: PointerFlags::MIDDLE_BUTTON_OR_WHEEL,
                x_position: 900,
                y_position: 20,
                number_of_wheel_rotation_units: 0,
            },
        );
        assert!(matches!(
            r.0.as_slice(),
            [MouseEvent::Move { x: 900, y: 20 }, MouseEvent::MiddleReleased]
        ));
    }
    #[test]
    fn wheel_ignores_coordinates_and_other_flags() {
        let mut r = Recorder::default();
        dispatch_mouse(
            &mut r,
            MousePdu {
                flags: PointerFlags::VERTICAL_WHEEL | PointerFlags::LEFT_BUTTON,
                x_position: 65535,
                y_position: 65535,
                number_of_wheel_rotation_units: -120,
            },
        );
        assert!(matches!(r.0.as_slice(), [MouseEvent::VerticalScroll { value: -120 }]));
    }
    #[test]
    fn extra_buttons_do_not_become_left_right() {
        let mut r = Recorder::default();
        dispatch_mouse_x(
            &mut r,
            MouseXPdu {
                flags: PointerXFlags::BUTTON1 | PointerXFlags::DOWN,
                x_position: 50,
                y_position: 60,
            },
        );
        assert!(matches!(
            r.0.as_slice(),
            [MouseEvent::Move { x: 50, y: 60 }, MouseEvent::Button4Pressed]
        ));
    }
}
