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
    self.serverLabel=label(@"正在读取服务状态…",15,YES);[root addArrangedSubview:self.serverLabel];
    self.unlock=button(@"验证管理员…",self,@selector(unlockAction:));self.unlock.identifier=@"manager.unlock";
    NSStackView *serverActions=[NSStackView stackViewWithViews:@[
        button(@"刷新",self,@selector(refreshAction:)),self.unlock,
        button(@"启动服务",self,@selector(startServer:)),button(@"重启服务",self,@selector(restartServer:)),
        button(@"停止服务",self,@selector(stopServer:)),button(@"配置用户…",self,@selector(configureUsers:))]];serverActions.spacing=10;[root addArrangedSubview:serverActions];
    self.scope=label(@"",12,NO);self.scope.textColor=NSColor.secondaryLabelColor;[root addArrangedSubview:self.scope];
    NSScrollView *scroll=[NSScrollView new];scroll.hasVerticalScroller=YES;scroll.borderType=NSBezelBorder;
    self.table=[NSTableView new];self.table.dataSource=self;self.table.delegate=self;self.table.rowHeight=48;self.table.usesAlternatingRowBackgroundColors=YES;
    NSArray *columns=@[@[@"username",@"账号",@130],@[@"desktop",@"桌面代理",@130],@[@"permissions",@"权限",@170],@[@"connection",@"RDP 连接",@120],@[@"mode",@"运行方式",@120],@[@"detail",@"状态说明",@300]];
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
    NSTextField *note=label(@"录屏和键鼠权限需要在对应账号中授权。可从该账号菜单栏的 ZMacRDS 打开系统设置。",12,NO);note.textColor=NSColor.secondaryLabelColor;[root addArrangedSubview:note];
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
    if([column isEqual:@"mode"])return !fresh ? @"—" : [row[@"background"] boolValue] ? @"后台运行" : @"诊断界面";
    if([row[@"capture_error"] length])return row[@"capture_error"];
    if(!fresh)return loaded ? @"正在等待桌面状态" : @"启动代理；若失败，请先在本机登录该账号";
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
            if([reply[@"setup_required"] boolValue]){self.serverLabel.stringValue=@"○ 尚未配置服务账号";self.message.stringValue=@"点击“配置用户…”完成首次设置。";}
            self.scope.stringValue=[reply[@"all_accounts"] boolValue] ? @"显示已配置的服务账号；系统服务及其他账号操作需要管理员验证。" : @"当前只显示本账号。验证管理员后可查看和管理其他服务账号。";
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
- (void)submitConfiguration:(NSNumber *)broker desktops:(NSArray *)desktops retried:(BOOL)retried {
    self.busy=YES;self.message.stringValue=@"正在保存配置并启动服务，请稍候…";
    NSMutableDictionary *request=[self request:@"configure" uid:nil];request[@"broker_uid"]=broker;request[@"desktop_uids"]=desktops;
    dispatch_async(dispatch_get_global_queue(QOS_CLASS_USER_INITIATED,0),^{
        NSDictionary *reply=zrRequest(request);dispatch_async(dispatch_get_main_queue(),^{
            self.busy=NO;
            if(!retried && [reply[@"error"] isEqual:@"authorization_required"]) {
                [self lockAuthorization];if([self authenticate])[self submitConfiguration:broker desktops:desktops retried:YES];return;
            }
            self.message.stringValue=reply[@"message"] ?: @"配置未完成，请刷新服务状态";[self refreshAction:nil];
        });
    });
}
- (void)showConfiguration:(NSDictionary *)info {
    NSArray *accounts=info[@"accounts"],*configured=info[@"desktop_uids"];
    if(!accounts.count){self.message.stringValue=@"请先在系统设置创建本地用户，再重新配置。";return;}
    NSAlert *alert=NSAlert.new;alert.messageText=@"配置服务账号";
    alert.informativeText=@"选择允许通过 RDP 使用的本地用户。已有账号和连接口令会保留；保存时 RDP 将重新连接，桌面应用会保留。新增用户需要先登录并授权录屏与键鼠。";
    [alert addButtonWithTitle:@"保存配置"];[alert addButtonWithTitle:@"取消"];
    NSView *accessory=[[NSView alloc] initWithFrame:NSMakeRect(0,0,580,390)];
    NSTextField *title=label(@"服务运行账号（不能同时作为远程桌面账号）",13,YES);title.frame=NSMakeRect(0,358,580,24);[accessory addSubview:title];
    NSPopUpButton *broker=[[NSPopUpButton alloc] initWithFrame:NSMakeRect(0,320,580,30) pullsDown:NO];
    NSNumber *current=info[@"broker_uid"]==NSNull.null ? nil : info[@"broker_uid"];
    for(NSDictionary *account in accounts){[broker addItemWithTitle:account[@"username"]];broker.lastItem.representedObject=account[@"uid"];if([account[@"uid"] isEqual:current])[broker selectItem:broker.lastItem];}
    broker.enabled=!current;[accessory addSubview:broker];
    NSTextField *desktops=label(@"远程桌面账号（最多 8 个，已有账号保留）",13,YES);desktops.frame=NSMakeRect(0,280,580,24);[accessory addSubview:desktops];
    NSScrollView *scroll=[[NSScrollView alloc] initWithFrame:NSMakeRect(0,0,580,270)];scroll.hasVerticalScroller=YES;scroll.borderType=NSBezelBorder;
    CGFloat height=MAX(270,accounts.count*30);NSView *document=[[NSView alloc] initWithFrame:NSMakeRect(0,0,550,height)];NSMutableArray<NSButton *> *choices=NSMutableArray.new;
    for(NSUInteger index=0;index<accounts.count;index++) {
        NSDictionary *account=accounts[index];NSButton *choice=[NSButton checkboxWithTitle:account[@"username"] target:nil action:nil];choice.frame=NSMakeRect(12,height-30*(index+1),520,28);choice.tag=[account[@"uid"] integerValue];
        BOOL already=[configured containsObject:account[@"uid"]];choice.state=already ? NSControlStateValueOn : NSControlStateValueOff;
        choice.enabled=!already && ![account[@"uid"] isEqual:current];[choices addObject:choice];[document addSubview:choice];
    }
    scroll.documentView=document;[accessory addSubview:scroll];alert.accessoryView=accessory;
    [alert beginSheetModalForWindow:self.window completionHandler:^(NSModalResponse response){
        if(response!=NSAlertFirstButtonReturn)return;
        NSMutableArray *selected=NSMutableArray.new;for(NSButton *choice in choices)if(choice.state==NSControlStateValueOn)[selected addObject:@(choice.tag)];
        NSNumber *service=broker.selectedItem.representedObject;
        if(!service || selected.count<1 || selected.count>8 || [selected containsObject:service]){self.message.stringValue=@"请选择 1–8 个桌面账号，并选择不同的服务运行账号。";return;}
        [self submitConfiguration:service desktops:selected retried:NO];
    }];
}
- (void)configureUsers:(id)sender {
    (void)sender;if(self.busy)return;
    if(!self.authorizationForm && ![self authenticate])return;
    self.busy=YES;NSDictionary *request=[self request:@"setup-info" uid:nil];
    dispatch_async(dispatch_get_global_queue(QOS_CLASS_USER_INITIATED,0),^{
        NSDictionary *reply=zrRequest(request);dispatch_async(dispatch_get_main_queue(),^{
            self.busy=NO;
            if([reply[@"ok"] boolValue])[self showConfiguration:reply];
            else {if([reply[@"error"] isEqual:@"authorization_required"])[self lockAuthorization];self.message.stringValue=reply[@"message"] ?: @"无法读取账号列表，请重新验证管理员。";}
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
            alert.informativeText=@"通过已核对服务器身份的安全隧道连接。这是独立的 RDP 口令；苹果屏幕共享仍使用 Mac 登录密码。";
            [alert addButtonWithTitle:@"关闭"];[alert addButtonWithTitle:@"复制登录信息"];
            NSStackView *fields=[NSStackView stackViewWithViews:@[]];fields.orientation=NSUserInterfaceLayoutOrientationVertical;fields.alignment=NSLayoutAttributeLeading;fields.spacing=10;
            for(NSString *key in @[@"endpoint",@"username"]) {NSTextField *field=label([NSString stringWithFormat:@"%@：%@",[key isEqual:@"endpoint"] ? @"连接地址" : @"用户名",reply[key]],13,NO);field.selectable=YES;[fields addArrangedSubview:field];}
            NSSecureTextField *password=[[NSSecureTextField alloc] initWithFrame:NSMakeRect(0,0,550,24)];password.stringValue=reply[@"password"];password.editable=NO;password.selectable=YES;password.placeholderString=@"RDP 口令";[fields addArrangedSubview:password];[password.widthAnchor constraintEqualToConstant:550].active=YES;
            NSTextField *fingerprint=label([@"证书 SHA-256：\n" stringByAppendingString:reply[@"sha256"]],12,NO);fingerprint.selectable=YES;fingerprint.maximumNumberOfLines=4;fingerprint.lineBreakMode=NSLineBreakByCharWrapping;[fields addArrangedSubview:fingerprint];[fingerprint.widthAnchor constraintEqualToConstant:550].active=YES;
            fields.frame=NSMakeRect(0,0,550,180);alert.accessoryView=fields;
            [alert beginSheetModalForWindow:self.window completionHandler:^(NSModalResponse response){
                if(response==NSAlertSecondButtonReturn){[NSPasteboard.generalPasteboard clearContents];[NSPasteboard.generalPasteboard setString:[NSString stringWithFormat:@"地址：%@\n用户名：%@\nRDP 口令：%@\n证书 SHA-256：%@",reply[@"endpoint"],reply[@"username"],reply[@"password"],reply[@"sha256"]] forType:NSPasteboardTypeString];}
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
- (void)startServer:(id)sender {(void)sender;[self operate:@"start-server" uid:nil confirmed:NO];}
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
