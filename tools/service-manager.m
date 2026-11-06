#import <AppKit/AppKit.h>
#import <Security/Authorization.h>
#import "service-protocol.h"

@interface Manager : NSObject <NSApplicationDelegate,NSTableViewDataSource,NSTableViewDelegate>
@property NSWindow *window;
@property NSTableView *table;
@property NSTextField *serverLabel,*message,*scope;
@property NSButton *unlock;
@property NSArray<NSDictionary *> *users;
@property NSDictionary *snapshot;
@property NSTimer *timer;
@property BOOL refreshing,busy;
@property AuthorizationRef authorization;
@property NSString *authorizationForm;
@property NSTimeInterval authorizationDeadline;
@end

static NSTextField *label(NSString *text,CGFloat size,BOOL bold) {
    NSTextField *field=[NSTextField labelWithString:text];
    field.font=bold ? [NSFont boldSystemFontOfSize:size] : [NSFont systemFontOfSize:size];
    return field;
}
static NSButton *button(NSString *title,id target,SEL action) {
    NSButton *b=[NSButton buttonWithTitle:title target:target action:action];b.bezelStyle=NSBezelStyleRounded;return b;
}
@implementation Manager
- (void)applicationDidFinishLaunching:(NSNotification *)notification {
    (void)notification;self.users=@[];
    self.window=[[NSWindow alloc] initWithContentRect:NSMakeRect(200,180,1060,650) styleMask:NSWindowStyleMaskTitled|NSWindowStyleMaskClosable|NSWindowStyleMaskMiniaturizable|NSWindowStyleMaskResizable backing:NSBackingStoreBuffered defer:NO];
    self.window.title=@"ZMacRDS 管理";self.window.minSize=NSMakeSize(960,620);self.window.releasedWhenClosed=NO;
    NSView *content=self.window.contentView;
    NSStackView *root=[NSStackView stackViewWithViews:@[]];root.orientation=NSUserInterfaceLayoutOrientationVertical;root.alignment=NSLayoutAttributeLeading;root.spacing=16;root.translatesAutoresizingMaskIntoConstraints=NO;
    [content addSubview:root];[NSLayoutConstraint activateConstraints:@[
        [root.leadingAnchor constraintEqualToAnchor:content.leadingAnchor constant:24],
        [root.trailingAnchor constraintEqualToAnchor:content.trailingAnchor constant:-24],
        [root.topAnchor constraintEqualToAnchor:content.topAnchor constant:24],
        [root.bottomAnchor constraintEqualToAnchor:content.bottomAnchor constant:-24]]];
    [root addArrangedSubview:label(@"远程桌面服务",26,YES)];
    [root addArrangedSubview:label(@"查看账号、桌面权限和 RDP 连接。断开连接会保留用户的桌面应用。",13,NO)];
    self.serverLabel=label(@"正在读取服务状态…",15,YES);self.serverLabel.maximumNumberOfLines=3;[root addArrangedSubview:self.serverLabel];[self.serverLabel.widthAnchor constraintEqualToAnchor:root.widthAnchor].active=YES;
    self.unlock=button(@"验证管理员…",self,@selector(unlockAction:));self.unlock.identifier=@"manager.unlock";
    NSStackView *serverActions=[NSStackView stackViewWithViews:@[
        button(@"刷新",self,@selector(refreshAction:)),self.unlock,
        button(@"启动服务",self,@selector(startServer:)),button(@"重启服务",self,@selector(restartServer:)),
        button(@"停止服务",self,@selector(stopServer:)),button(@"连接设置…",self,@selector(networkSettings:)),button(@"系统共享设置…",self,@selector(sharingSettings:))]];serverActions.spacing=10;[root addArrangedSubview:serverActions];
    self.scope=label(@"",12,NO);self.scope.textColor=NSColor.secondaryLabelColor;[root addArrangedSubview:self.scope];
    NSScrollView *scroll=[NSScrollView new];scroll.hasVerticalScroller=YES;scroll.borderType=NSBezelBorder;
    self.table=[NSTableView new];self.table.dataSource=self;self.table.delegate=self;self.table.rowHeight=48;self.table.usesAlternatingRowBackgroundColors=YES;
    NSArray *columns=@[@[@"username",@"账号",@130],@[@"desktop",@"桌面代理",@130],@[@"permissions",@"权限",@170],@[@"connection",@"RDP 连接",@120],@[@"mode",@"登录状态",@120],@[@"detail",@"状态说明",@300]];
    for(NSArray *c in columns){NSTableColumn *column=[[NSTableColumn alloc] initWithIdentifier:c[0]];column.title=c[1];column.width=[c[2] doubleValue];column.minWidth=90;[self.table addTableColumn:column];}
    scroll.documentView=self.table;[root addArrangedSubview:scroll];
    scroll.translatesAutoresizingMaskIntoConstraints=NO;
    [scroll.widthAnchor constraintEqualToAnchor:root.widthAnchor].active=YES;
    [scroll.heightAnchor constraintGreaterThanOrEqualToConstant:220].active=YES;
    NSStackView *userActions=[NSStackView stackViewWithViews:@[
        button(@"断开所选账号的 RDP",self,@selector(disconnectUser:)),
        button(@"启动桌面代理",self,@selector(startUser:)),
        button(@"重启桌面代理",self,@selector(restartUser:)),
        button(@"停止桌面代理",self,@selector(stopUser:)),button(@"连接信息…",self,@selector(connectionInfo:))]];userActions.spacing=10;[root addArrangedSubview:userActions];
    NSTextField *note=label(@"首次使用需在各账号授权录屏和键鼠。开启系统屏幕共享并允许对应用户后，RDP 可按需启动未登录的用户桌面。",12,NO);note.textColor=NSColor.secondaryLabelColor;note.maximumNumberOfLines=2;[root addArrangedSubview:note];[note.widthAnchor constraintEqualToAnchor:root.widthAnchor].active=YES;
    self.message=label(@"",12,NO);self.message.lineBreakMode=NSLineBreakByWordWrapping;self.message.maximumNumberOfLines=3;[root addArrangedSubview:self.message];[self.message.widthAnchor constraintEqualToAnchor:root.widthAnchor].active=YES;
    NSMenu *menu=[NSMenu new];NSMenuItem *app=[NSMenuItem new];NSMenu *submenu=[NSMenu new];app.submenu=submenu;[menu addItem:app];
    [submenu addItemWithTitle:@"退出 ZMacRDS 管理" action:@selector(terminate:) keyEquivalent:@"q"];NSApp.mainMenu=menu;
    [self.window center];[self.window makeKeyAndOrderFront:nil];[NSApp activateIgnoringOtherApps:YES];
    self.timer=[NSTimer scheduledTimerWithTimeInterval:3 target:self selector:@selector(refreshAction:) userInfo:nil repeats:YES];[self refreshAction:nil];
}
- (BOOL)applicationShouldTerminateAfterLastWindowClosed:(NSApplication *)app {(void)app;return YES;}
- (void)applicationWillTerminate:(NSNotification *)notification {
    (void)notification;[self.timer invalidate];if(self.authorization)AuthorizationFree(self.authorization,kAuthorizationFlagDestroyRights);self.authorization=NULL;self.authorizationForm=nil;
}
- (NSInteger)numberOfRowsInTableView:(NSTableView *)tableView {(void)tableView;return self.users.count;}
- (NSString *)textFor:(NSDictionary *)row column:(NSString *)column {
    BOOL fresh=[row[@"fresh"] boolValue];BOOL loaded=[row[@"job"][@"loaded"] boolValue];
    if([column isEqual:@"username"])return row[@"username"];
    if([column isEqual:@"desktop"])return fresh ? @"运行中" : loaded ? @"启动中或暂无响应" : @"未运行";
    if([column isEqual:@"permissions"]) {
        if(!fresh)return @"等待代理报告";
        if(!row[@"screen_capture"] || !row[@"accessibility"])return @"等待代理升级";
        if([row[@"screen_capture"] boolValue] && [row[@"accessibility"] boolValue])return @"录屏、键鼠均已授权";
        return [row[@"screen_capture"] boolValue] ? @"需要键鼠授权" : @"需要录屏授权";
    }
    if([column isEqual:@"connection"])return !fresh || !row[@"rdp_connected"] ? @"未知" : [row[@"rdp_connected"] boolValue] ? @"已连接" : @"未连接";
    if([column isEqual:@"mode"])return [row[@"logged_in"] boolValue] ? @"已登录 Mac" : @"尚未登录桌面";
    if([row[@"capture_error"] length])return row[@"capture_error"];
    if([row[@"login_error"] length])return row[@"login_error"];
    if(!fresh)return ![row[@"logged_in"] boolValue] ? @"连接时尝试自动登录；首次使用需完成系统设置和授权" : loaded ? @"正在等待桌面状态" : @"桌面已登录，启动代理以准备连接";
    if(!row[@"capturing"])return @"升级桌面代理后可显示完整状态";
    if([row[@"capture_paused"] boolValue])return @"采集暂停；等待正常唤醒或解锁";
    return [row[@"capturing"] boolValue] ? @"桌面采集正常" : @"等待采集或系统授权";
}
- (NSView *)tableView:(NSTableView *)tableView viewForTableColumn:(NSTableColumn *)column row:(NSInteger)index {
    (void)tableView;NSTextField *text=label([self textFor:self.users[index] column:column.identifier] ?: @"—",12,[column.identifier isEqual:@"username"]);
    text.lineBreakMode=NSLineBreakByTruncatingTail;text.toolTip=text.stringValue;return text;
}
- (NSMutableDictionary *)request:(NSString *)action uid:(NSNumber *)uid {
    NSMutableDictionary *request=[@{@"action":action} mutableCopy];if(uid)request[@"uid"]=uid;if(self.authorizationForm)request[@"authorization"]=self.authorizationForm;return request;
}
- (void)refreshAction:(id)sender {
    (void)sender;if(self.refreshing || self.busy)return;self.refreshing=YES;
    if(self.authorizationForm && NSProcessInfo.processInfo.systemUptime>=self.authorizationDeadline) {
        [self lockAuthorization];self.message.stringValue=@"管理员验证已过期，操作时可重新验证";
    }
    NSDictionary *request=[self request:@"status" uid:nil];
    dispatch_async(dispatch_get_global_queue(QOS_CLASS_USER_INITIATED,0),^{
        NSDictionary *reply=zrRequest(request);dispatch_async(dispatch_get_main_queue(),^{
            self.refreshing=NO;
            if(![reply[@"ok"] boolValue]){self.serverLabel.stringValue=@"管理服务暂不可用";self.serverLabel.textColor=NSColor.systemRedColor;self.message.stringValue=reply[@"message"] ?: @"请检查安装与服务状态";return;}
            NSNumber *selected=self.table.selectedRow>=0 && self.table.selectedRow<(NSInteger)self.users.count ? self.users[self.table.selectedRow][@"uid"] : nil;
            self.snapshot=reply;self.users=reply[@"users"] ?: @[];[self.table reloadData];
            if(selected)for(NSUInteger i=0;i<self.users.count;i++)if([selected isEqual:self.users[i][@"uid"]])[self.table selectRowIndexes:[NSIndexSet indexSetWithIndex:i] byExtendingSelection:NO];
            if(self.table.selectedRow<0 && self.users.count)[self.table selectRowIndexes:[NSIndexSet indexSetWithIndex:0] byExtendingSelection:NO];
            NSDictionary *broker=reply[@"broker"];BOOL running=[broker[@"state"] isEqual:@"running"];
            self.serverLabel.stringValue=running ? @"● RDP 服务运行中" : [broker[@"loaded"] boolValue] ? @"● RDP 服务正在启动" : @"○ RDP 服务已停止";
            self.serverLabel.textColor=running ? NSColor.systemGreenColor : NSColor.secondaryLabelColor;
            if([reply[@"setup_required"] boolValue]){self.serverLabel.stringValue=@"○ 服务尚未启动";self.message.stringValue=@"点击“启动服务”。软件自动识别此 Mac 的本地用户，使用原有 Mac 登录密码。";}
            else if([reply[@"lan_port"] isKindOfClass:NSNumber.class]) {
                NSMutableArray *endpoints=NSMutableArray.new;
                for(NSString *address in reply[@"addresses"])[endpoints addObject:[reply[@"lan_port"] intValue]==3389 ? address : [NSString stringWithFormat:@"%@:%@",address,reply[@"lan_port"]]];
                self.serverLabel.stringValue=[self.serverLabel.stringValue stringByAppendingFormat:@"  ·  %@",endpoints.count ? [endpoints componentsJoinedByString:@" / "] : @"等待局域网地址"];
            }
            self.scope.stringValue=[reply[@"all_accounts"] boolValue] ? @"本地用户及其桌面登录状态；已连接显示当前 RDP 会话。系统操作需要管理员验证。" : @"当前只显示本账号。验证管理员后可查看和管理其他服务账号。";
        });
    });
}
- (BOOL)authenticate {
    if(!self.authorization && AuthorizationCreate(NULL,kAuthorizationEmptyEnvironment,kAuthorizationFlagDefaults,&_authorization)!=errAuthorizationSuccess)return NO;
    AuthorizationItem item={ZR_CONTROL_RIGHT,0,NULL,0};AuthorizationRights rights={1,&item};
    OSStatus status=AuthorizationCopyRights(self.authorization,&rights,kAuthorizationEmptyEnvironment,kAuthorizationFlagInteractionAllowed|kAuthorizationFlagExtendRights|kAuthorizationFlagPreAuthorize,NULL);
    if(status!=errAuthorizationSuccess){self.message.stringValue=status==errAuthorizationCanceled ? @"已取消管理员验证" : @"管理员验证未完成";return NO;}
    AuthorizationExternalForm form;
    if(AuthorizationMakeExternalForm(self.authorization,&form)!=errAuthorizationSuccess)return NO;
    self.authorizationForm=[[NSData dataWithBytes:&form length:sizeof(form)] base64EncodedStringWithOptions:0];
    self.authorizationDeadline=NSProcessInfo.processInfo.systemUptime+300;
    volatile uint8_t *p=(volatile uint8_t *)&form;for(size_t i=0;i<sizeof(form);i++)p[i]=0;
    self.unlock.title=@"锁定管理";return YES;
}
- (void)lockAuthorization {
    if(self.authorization)AuthorizationFree(self.authorization,kAuthorizationFlagDestroyRights);
    self.authorization=NULL;self.authorizationForm=nil;self.authorizationDeadline=0;self.unlock.title=@"验证管理员…";
}
- (void)unlockAction:(id)sender {
    (void)sender;
    if(self.authorizationForm){[self lockAuthorization];self.message.stringValue=@"已锁定管理员操作";}
    else if([self authenticate])self.message.stringValue=@"管理员验证完成";
    [self refreshAction:nil];
}
- (void)applyConnectionSettings:(BOOL)enabled port:(NSNumber *)port start:(BOOL)start retried:(BOOL)retried {
    if(self.busy)return;
    if(!self.authorizationForm && ![self authenticate])return;
    self.busy=YES;self.message.stringValue=start ? @"正在启动服务并识别本地用户…" : @"正在保存连接设置…";
    NSMutableDictionary *request=[self request:start ? @"configure-local" : @"configure-network" uid:nil];
    request[@"enabled"]=@(enabled);request[@"port"]=port;
    dispatch_async(dispatch_get_global_queue(QOS_CLASS_USER_INITIATED,0),^{
        NSDictionary *reply=zrRequest(request);dispatch_async(dispatch_get_main_queue(),^{
            self.busy=NO;
            if(!retried && [reply[@"error"] isEqual:@"authorization_required"]) {
                [self lockAuthorization];if([self authenticate])[self applyConnectionSettings:enabled port:port start:start retried:YES];return;
            }
            self.message.stringValue=reply[@"message"] ?: @"操作未完成，请刷新服务状态";[self refreshAction:nil];
        });
    });
}
- (void)sharingSettings:(id)sender {(void)sender;[NSWorkspace.sharedWorkspace openURL:[NSURL URLWithString:@"x-apple.systempreferences:com.apple.Sharing-Settings.extension"]];}
- (void)networkSettings:(id)sender {
    (void)sender;if(self.busy)return;
    if(!self.authorizationForm && ![self authenticate])return;
    self.busy=YES;NSDictionary *request=[self request:@"setup-info" uid:nil];
    dispatch_async(dispatch_get_global_queue(QOS_CLASS_USER_INITIATED,0),^{
        NSDictionary *reply=zrRequest(request);dispatch_async(dispatch_get_main_queue(),^{
            self.busy=NO;
            if(![reply[@"ok"] boolValue]){self.message.stringValue=reply[@"message"] ?: @"无法读取连接设置";return;}
            NSDictionary *settings=reply[@"network"];
            NSAlert *alert=NSAlert.new;alert.messageText=@"客户端连接设置";
            alert.informativeText=@"局域网内的 Mac 和 Windows 可直接输入服务器 IP、本地用户名及 Mac 登录密码连接。默认端口 3389 无需在客户端额外填写端口。已有连接可能需要重连。";
            [alert addButtonWithTitle:@"保存"];[alert addButtonWithTitle:@"取消"];
            NSView *view=[[NSView alloc] initWithFrame:NSMakeRect(0,0,560,160)];
            NSButton *enabled=[NSButton checkboxWithTitle:@"允许局域网直接连接 RDP" target:nil action:nil];enabled.frame=NSMakeRect(0,120,560,28);
            enabled.state=[settings[@"enabled"] boolValue] || [self.snapshot[@"setup_required"] boolValue] ? NSControlStateValueOn : NSControlStateValueOff;[view addSubview:enabled];
            NSTextField *caption=label(@"RDP 端口",13,YES);caption.frame=NSMakeRect(0,82,100,24);[view addSubview:caption];
            NSTextField *port=[[NSTextField alloc] initWithFrame:NSMakeRect(110,82,120,24)];port.stringValue=[settings[@"port"] stringValue] ?: @"3389";[view addSubview:port];
            NSTextField *addresses=label([@"此 Mac 的地址：" stringByAppendingString:[settings[@"addresses"] count] ? [settings[@"addresses"] componentsJoinedByString:@"、"] : @"暂无可用局域网 IPv4 地址"],12,NO);
            addresses.frame=NSMakeRect(0,14,560,55);addresses.maximumNumberOfLines=3;addresses.selectable=YES;[view addSubview:addresses];alert.accessoryView=view;
            [alert beginSheetModalForWindow:self.window completionHandler:^(NSModalResponse response){
                if(response!=NSAlertFirstButtonReturn)return;
                NSString *value=port.stringValue;NSCharacterSet *digits=NSCharacterSet.decimalDigitCharacterSet;
                if(!value.length || [value rangeOfCharacterFromSet:digits.invertedSet].location!=NSNotFound || value.integerValue<1024 || value.integerValue>65535 || value.integerValue==3390){self.message.stringValue=@"请输入 1024–65535 的端口，3390 为保留端口。";return;}
                [self applyConnectionSettings:enabled.state==NSControlStateValueOn port:@(value.integerValue) start:[self.snapshot[@"setup_required"] boolValue] retried:NO];
            }];
        });
    });
}
- (void)connectionInfoUID:(NSNumber *)uid retried:(BOOL)retried {
    self.busy=YES;NSDictionary *request=[self request:@"connection-info" uid:uid];
    dispatch_async(dispatch_get_global_queue(QOS_CLASS_USER_INITIATED,0),^{
        NSDictionary *reply=zrRequest(request);dispatch_async(dispatch_get_main_queue(),^{
            self.busy=NO;
            if(!retried && [reply[@"error"] isEqual:@"authorization_required"]){[self lockAuthorization];if([self authenticate])[self connectionInfoUID:uid retried:YES];return;}
            if(![reply[@"ok"] boolValue]){self.message.stringValue=reply[@"message"] ?: @"无法读取连接信息";return;}
            NSAlert *alert=NSAlert.new;alert.messageText=@"RDP 连接信息";
            BOOL local=[reply[@"local_auth"] boolValue];
            alert.informativeText=local ? @"在客户端输入下方服务器地址、本地用户名和该用户的 Mac 登录密码。首次连接核对证书指纹。" : @"旧版连接仍使用独立口令；点击主窗口的启动服务可切换为 Mac 本地账号登录。";
            [alert addButtonWithTitle:@"关闭"];[alert addButtonWithTitle:@"复制登录信息"];[alert addButtonWithTitle:@"导出 RDP 文件…"];
            NSStackView *fields=[NSStackView stackViewWithViews:@[]];fields.orientation=NSUserInterfaceLayoutOrientationVertical;fields.alignment=NSLayoutAttributeLeading;fields.spacing=10;
            for(NSString *key in @[@"endpoint",@"username"]) {NSTextField *field=label([NSString stringWithFormat:@"%@：%@",[key isEqual:@"endpoint"] ? @"连接地址" : @"用户名",reply[key]],13,NO);field.selectable=YES;[fields addArrangedSubview:field];}
            NSSecureTextField *password=[[NSSecureTextField alloc] initWithFrame:NSMakeRect(0,0,550,24)];password.stringValue=reply[@"password"];password.editable=NO;password.selectable=YES;password.placeholderString=@"RDP 口令";if(local)[fields addArrangedSubview:label(@"密码：使用此用户的 Mac 登录密码",13,YES)];else {[fields addArrangedSubview:password];[password.widthAnchor constraintEqualToConstant:550].active=YES;}
            NSTextField *fingerprint=label([@"证书 SHA-256：\n" stringByAppendingString:reply[@"sha256"]],12,NO);fingerprint.selectable=YES;fingerprint.maximumNumberOfLines=4;fingerprint.lineBreakMode=NSLineBreakByCharWrapping;[fields addArrangedSubview:fingerprint];[fingerprint.widthAnchor constraintEqualToConstant:550].active=YES;
            fields.frame=NSMakeRect(0,0,550,180);alert.accessoryView=fields;
            [alert beginSheetModalForWindow:self.window completionHandler:^(NSModalResponse response){
                if(response==NSAlertSecondButtonReturn){[NSPasteboard.generalPasteboard clearContents];[NSPasteboard.generalPasteboard setString:[NSString stringWithFormat:@"地址：%@\n用户名：%@\nRDP 口令：%@\n证书 SHA-256：%@",reply[@"endpoint"],reply[@"username"],local ? @"使用该用户的 Mac 登录密码" : reply[@"password"],reply[@"sha256"]] forType:NSPasteboardTypeString];}
                if(response==NSAlertThirdButtonReturn) {
                    NSSavePanel *panel=NSSavePanel.savePanel;panel.nameFieldStringValue=[NSString stringWithFormat:@"ZMacRDS-%@.rdp",reply[@"username"]];
                    [panel beginSheetModalForWindow:self.window completionHandler:^(NSModalResponse result){
                        if(result!=NSModalResponseOK)return;
                        NSString *profile=[NSString stringWithFormat:@"full address:s:%@\r\nusername:s:%@\r\ndesktopwidth:i:1920\r\ndesktopheight:i:1200\r\nscreen mode id:i:1\r\ndynamic resolution:i:0\r\nsmart sizing:i:1\r\naudiomode:i:0\r\naudiocapturemode:i:0\r\nredirectclipboard:i:0\r\ndrivestoredirect:s:\r\nprompt for credentials:i:1\r\nauthentication level:i:2\r\n",reply[@"endpoint"],reply[@"username"]];
                        NSError *error=nil;if(![profile writeToURL:panel.URL atomically:YES encoding:NSUTF8StringEncoding error:&error])self.message.stringValue=@"连接文件未保存，请检查目标文件夹。";
                    }];
                }
                password.stringValue=@"";
            }];
        });
    });
}
- (void)connectionInfo:(id)sender {
    (void)sender;if(self.busy)return;
    NSInteger row=self.table.selectedRow;if(row<0 || row>=(NSInteger)self.users.count){self.message.stringValue=@"请先选择账号";return;}
    [self connectionInfoUID:self.users[row][@"uid"] retried:NO];
}
- (void)operate:(NSString *)action uid:(NSNumber *)uid confirmed:(BOOL)confirmed {
    if(self.busy)return;
    BOOL server=[action hasSuffix:@"-server"];
    if(server && ![action isEqual:@"start-server"] && !confirmed) {
        NSAlert *alert=[NSAlert new];alert.messageText=[action isEqual:@"stop-server"] ? @"停止 RDP 服务？" : @"重启 RDP 服务？";
        alert.informativeText=@"所有 RDP 连接会中断，用户的桌面应用会保留。操作完成后可能需要重新连接。";
        [alert addButtonWithTitle:@"继续"];[alert addButtonWithTitle:@"取消"];
        [alert beginSheetModalForWindow:self.window completionHandler:^(NSModalResponse response){if(response==NSAlertFirstButtonReturn)[self operate:action uid:uid confirmed:YES];}];return;
    }
    self.busy=YES;self.message.stringValue=@"正在处理…";
    NSDictionary *request=[self request:action uid:uid];
    dispatch_async(dispatch_get_global_queue(QOS_CLASS_USER_INITIATED,0),^{
        NSDictionary *reply=zrRequest(request);dispatch_async(dispatch_get_main_queue(),^{
            self.busy=NO;
            if([reply[@"error"] isEqual:@"authorization_required"]) {
                [self lockAuthorization];
                if([self authenticate])[self operate:action uid:uid confirmed:YES];return;
            }
            self.message.stringValue=reply[@"message"] ?: ([reply[@"ok"] boolValue] ? @"操作完成" : @"操作未完成");
            [self refreshAction:nil];
        });
    });
}
- (void)userAction:(NSString *)action {
    NSInteger index=self.table.selectedRow;
    if(index<0 || index>=(NSInteger)self.users.count){self.message.stringValue=@"请先选择账号";return;}
    [self operate:action uid:self.users[index][@"uid"] confirmed:NO];
}
- (void)startServer:(id)sender {
    (void)sender;id port=self.snapshot[@"lan_port"];
    BOOL existing=[self.snapshot[@"local_auth"] boolValue];
    [self applyConnectionSettings:!existing || [port isKindOfClass:NSNumber.class] port:[port isKindOfClass:NSNumber.class] ? port : @3389 start:YES retried:NO];
}
- (void)stopServer:(id)sender {(void)sender;[self operate:@"stop-server" uid:nil confirmed:NO];}
- (void)restartServer:(id)sender {(void)sender;[self operate:@"restart-server" uid:nil confirmed:NO];}
- (void)disconnectUser:(id)sender {(void)sender;[self userAction:@"disconnect-rdp"];}
- (void)startUser:(id)sender {(void)sender;[self userAction:@"start-agent"];}
- (void)stopUser:(id)sender {(void)sender;[self userAction:@"stop-agent"];}
- (void)restartUser:(id)sender {(void)sender;[self userAction:@"restart-agent"];}
@end
int main(void) {@autoreleasepool {
    [NSApplication sharedApplication];Manager *delegate=[Manager new];NSApp.delegate=delegate;
    [NSApp setActivationPolicy:NSApplicationActivationPolicyRegular];[NSApp run];return 0;
}}
