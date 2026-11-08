#import <AppKit/AppKit.h>
#import <Security/Authorization.h>
#import "service-protocol.h"
#include <fcntl.h>

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
@property NSPanel *setupPanel;
@property NSTextField *setupServer,*setupScreen,*setupInput,*setupResult,*setupMessage;
@property NSButton *setupScreenButton,*setupInputButton,*setupApplyButton,*setupStartButton;
@property BOOL setupAutoPresented;
@property NSTextField *webLabel,*webHint;
@property NSStackView *webSection;
@property NSButton *webStart,*webStop,*webCopy,*webConfigure,*webExport;
@property NSPanel *webPanel;
@property NSPopUpButton *webAddress;
@property NSTextField *webSubnet,*webHost,*webPort,*webFormMessage;
@property NSButton *webCertButton,*webKeyButton;
@property NSButton *webAdvanced;
@property NSStackView *webAdvancedFields;
@property NSTextField *webMaxSessions;
@property NSURL *webCertificate,*webPrivateKey;
@end

static NSTextField *label(NSString *text,CGFloat size,BOOL bold) {
    NSTextField *field=[NSTextField labelWithString:text];
    field.font=bold ? [NSFont boldSystemFontOfSize:size] : [NSFont systemFontOfSize:size];
    return field;
}
static NSButton *button(NSString *title,id target,SEL action) {
    NSButton *b=[NSButton buttonWithTitle:title target:target action:action];b.bezelStyle=NSBezelStyleRounded;return b;
}
static NSData *readPEM(NSURL *url,size_t maximum) {
    if(!url.isFileURL)return nil;
    int fd=open(url.fileSystemRepresentation,O_RDONLY|O_NONBLOCK);
    if(fd<0)return nil;
    struct stat st;NSMutableData *data=nil;
    if(fstat(fd,&st)==0 && S_ISREG(st.st_mode) && st.st_size>0 && (uint64_t)st.st_size<=maximum) {
        data=NSMutableData.new;unsigned char bytes[4096];ssize_t count;
        while((count=read(fd,bytes,MIN(sizeof(bytes),maximum+1-data.length)))>0) {
            [data appendBytes:bytes length:(size_t)count];
            if(data.length>maximum){data=nil;break;}
        }
        if(count<0 || data.length==0)data=nil;
    }
    close(fd);return data;
}
@implementation Manager
- (void)applicationDidFinishLaunching:(NSNotification *)notification {
    (void)notification;self.users=@[];
    self.window=[[NSWindow alloc] initWithContentRect:NSMakeRect(200,180,1060,750) styleMask:NSWindowStyleMaskTitled|NSWindowStyleMaskClosable|NSWindowStyleMaskMiniaturizable|NSWindowStyleMaskResizable backing:NSBackingStoreBuffered defer:NO];
    self.window.title=@"ZMacRDS 管理";self.window.minSize=NSMakeSize(960,680);self.window.releasedWhenClosed=NO;
    NSView *content=self.window.contentView;
    NSStackView *root=[NSStackView stackViewWithViews:@[]];root.orientation=NSUserInterfaceLayoutOrientationVertical;root.alignment=NSLayoutAttributeLeading;root.spacing=16;root.translatesAutoresizingMaskIntoConstraints=NO;
    [content addSubview:root];[NSLayoutConstraint activateConstraints:@[
        [root.leadingAnchor constraintEqualToAnchor:content.leadingAnchor constant:24],
        [root.trailingAnchor constraintEqualToAnchor:content.trailingAnchor constant:-24],
        [root.topAnchor constraintEqualToAnchor:content.topAnchor constant:24],
        [root.bottomAnchor constraintEqualToAnchor:content.bottomAnchor constant:-24]]];
    [root addArrangedSubview:label(@"远程桌面服务",26,YES)];
    [root addArrangedSubview:label(@"配置访问地址、检查权限和查看当前连接。断开连接会保留用户的桌面应用。",13,NO)];
    self.webSection=[NSStackView stackViewWithViews:@[]];self.webSection.orientation=NSUserInterfaceLayoutOrientationVertical;self.webSection.alignment=NSLayoutAttributeLeading;self.webSection.spacing=8;
    self.webLabel=label(@"",16,YES);self.webLabel.selectable=YES;
    self.webHint=label(@"",12,NO);self.webHint.textColor=NSColor.secondaryLabelColor;self.webHint.maximumNumberOfLines=2;
    self.webStart=button(@"启动网页服务",self,@selector(startWeb:));
    self.webStop=button(@"停止网页服务",self,@selector(stopWeb:));
    self.webCopy=button(@"复制网页地址",self,@selector(copyWeb:));
    self.webExport=button(@"导出局域网证书…",self,@selector(exportWeb:));
    self.webConfigure=button(@"网页设置…",self,@selector(showWebSettings:));
    [self.webSection addArrangedSubview:self.webLabel];[self.webSection addArrangedSubview:self.webHint];
    NSStackView *webActions=[NSStackView stackViewWithViews:@[self.webConfigure,self.webStart,self.webStop,self.webCopy,self.webExport]];
    webActions.spacing=10;[self.webSection addArrangedSubview:webActions];
    [root addArrangedSubview:self.webSection];self.webSection.hidden=YES;
    [self.webSection.widthAnchor constraintEqualToAnchor:root.widthAnchor].active=YES;
    [self.webHint.widthAnchor constraintEqualToAnchor:self.webSection.widthAnchor].active=YES;
    self.serverLabel=label(@"正在读取服务状态…",15,YES);self.serverLabel.maximumNumberOfLines=3;[root addArrangedSubview:self.serverLabel];[self.serverLabel.widthAnchor constraintEqualToAnchor:root.widthAnchor].active=YES;
    self.unlock=button(@"验证管理员…",self,@selector(unlockAction:));self.unlock.identifier=@"manager.unlock";
    NSStackView *serverActions=[NSStackView stackViewWithViews:@[
        button(@"刷新",self,@selector(refreshAction:)),self.unlock,
        button(@"首次配置与权限…",self,@selector(showSetup:)),button(@"RDP 连接设置…",self,@selector(networkSettings:)),
        button(@"启动 RDP",self,@selector(startServer:)),button(@"停止 RDP",self,@selector(stopServer:))]];serverActions.spacing=10;[root addArrangedSubview:serverActions];
    self.scope=label(@"",12,NO);self.scope.textColor=NSColor.secondaryLabelColor;[root addArrangedSubview:self.scope];
    NSScrollView *scroll=[NSScrollView new];scroll.hasVerticalScroller=YES;scroll.borderType=NSBezelBorder;
    self.table=[NSTableView new];self.table.dataSource=self;self.table.delegate=self;self.table.rowHeight=48;self.table.usesAlternatingRowBackgroundColors=YES;
    NSArray *columns=@[@[@"username",@"账号",@115],@[@"desktop",@"桌面代理",@115],@[@"permissions",@"权限",@165],@[@"connection",@"远程连接",@115],@[@"mode",@"登录状态",@120],@[@"detail",@"状态说明",@300]];
    for(NSArray *c in columns){NSTableColumn *column=[[NSTableColumn alloc] initWithIdentifier:c[0]];column.title=c[1];column.width=[c[2] doubleValue];column.minWidth=90;[self.table addTableColumn:column];}
    scroll.documentView=self.table;[root addArrangedSubview:scroll];
    scroll.translatesAutoresizingMaskIntoConstraints=NO;
    [scroll.widthAnchor constraintEqualToAnchor:root.widthAnchor].active=YES;
    [scroll.heightAnchor constraintGreaterThanOrEqualToConstant:220].active=YES;
    NSStackView *userActions=[NSStackView stackViewWithViews:@[
        button(@"断开所选账号",self,@selector(disconnectUser:)),
        button(@"启动桌面代理",self,@selector(startUser:)),
        button(@"重启桌面代理",self,@selector(restartUser:)),
        button(@"停止桌面代理",self,@selector(stopUser:)),button(@"连接信息…",self,@selector(connectionInfo:))]];userActions.spacing=10;[root addArrangedSubview:userActions];
    NSTextField *note=label(@"各账号首次使用时需授权录屏和键鼠。若需为尚未登录的账号打开桌面，请在系统屏幕共享中单独允许对应用户。",12,NO);note.textColor=NSColor.secondaryLabelColor;note.maximumNumberOfLines=2;[root addArrangedSubview:note];[note.widthAnchor constraintEqualToAnchor:root.widthAnchor].active=YES;
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
        return [row[@"screen_capture"] boolValue] ? @"需要键鼠授权" : [row[@"accessibility"] boolValue] ? @"需要录屏授权" : @"需要录屏、键鼠授权";
    }
    if([column isEqual:@"connection"]) {
        NSUInteger count=[row[@"web_connections"] unsignedIntegerValue];
        if(count)return [NSString stringWithFormat:@"网页 · %lu",count];
        if(fresh && [row[@"rdp_connected"] boolValue])return @"桌面已连接";
        if(row[@"web_connections"])return @"未连接";
        return !fresh || !row[@"rdp_connected"] ? @"未知" : @"未连接";
    }
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
            if(![reply[@"ok"] boolValue]){self.serverLabel.stringValue=@"管理服务暂不可用";self.serverLabel.textColor=NSColor.systemRedColor;self.message.stringValue=reply[@"message"] ?: @"请检查安装与服务状态";self.webStart.enabled=self.webStop.enabled=self.webCopy.enabled=self.webConfigure.enabled=NO;return;}
            NSNumber *selected=self.table.selectedRow>=0 && self.table.selectedRow<(NSInteger)self.users.count ? self.users[self.table.selectedRow][@"uid"] : nil;
            self.snapshot=reply;self.users=reply[@"users"] ?: @[];[self.table reloadData];
            if(selected)for(NSUInteger i=0;i<self.users.count;i++)if([selected isEqual:self.users[i][@"uid"]])[self.table selectRowIndexes:[NSIndexSet indexSetWithIndex:i] byExtendingSelection:NO];
            if(self.table.selectedRow<0 && self.users.count)[self.table selectRowIndexes:[NSIndexSet indexSetWithIndex:0] byExtendingSelection:NO];
            NSDictionary *broker=reply[@"broker"];BOOL running=[broker[@"state"] isEqual:@"running"];
            self.serverLabel.stringValue=running ? @"● RDP 服务运行中" : [broker[@"loaded"] boolValue] ? @"● RDP 服务正在启动" : @"○ RDP 服务已停止";
            self.serverLabel.textColor=running ? NSColor.systemGreenColor : NSColor.secondaryLabelColor;
            if([reply[@"setup_required"] boolValue])self.serverLabel.stringValue=@"○ 桌面服务尚未启动";
            else if([reply[@"lan_port"] isKindOfClass:NSNumber.class]) {
                NSMutableArray *endpoints=NSMutableArray.new;
                for(NSString *address in reply[@"addresses"])[endpoints addObject:[reply[@"lan_port"] intValue]==3389 ? address : [NSString stringWithFormat:@"%@:%@",address,reply[@"lan_port"]]];
                self.serverLabel.stringValue=[self.serverLabel.stringValue stringByAppendingFormat:@"  ·  %@",endpoints.count ? [endpoints componentsJoinedByString:@" / "] : @"等待局域网地址"];
            }
            self.scope.stringValue=[reply[@"all_accounts"] boolValue] ? @"本地账号、桌面权限和远程连接。变更服务需要管理员验证。" : @"当前只显示本账号。验证管理员后可查看和管理其他服务账号。";
            [self updateWeb];
            [self updateSetup];
            NSDictionary *own=[self ownDesktop];
            if(!self.setupAutoPresented && ([reply[@"setup_required"] boolValue] ||
               ([own[@"fresh"] boolValue] && (![own[@"screen_capture"] boolValue] || ![own[@"accessibility"] boolValue])))) {
                self.setupAutoPresented=YES;[self showSetup:nil];
            }
        });
    });
}
- (NSDictionary *)ownDesktop {
    for(NSDictionary *row in self.users)if([row[@"uid"] unsignedIntValue]==getuid())return row;
    return nil;
}
- (void)updateSetup {
    if(!self.setupPanel)return;
    NSDictionary *own=[self ownDesktop];BOOL fresh=[own[@"fresh"] boolValue];
    BOOL screen=fresh && [own[@"screen_capture"] boolValue],input=fresh && [own[@"accessibility"] boolValue];
    BOOL running=[self.snapshot[@"broker"][@"state"] isEqual:@"running"];
    self.setupServer.stringValue=self.serverLabel.stringValue;
    self.setupScreen.stringValue=screen ? @"✓ 屏幕录制已生效" : @"○ 屏幕录制尚未生效";
    self.setupInput.stringValue=input ? @"✓ 远程键鼠权限已生效" : @"○ 远程键鼠权限尚未生效";
    self.setupScreenButton.enabled=fresh && !self.busy;
    self.setupInputButton.enabled=fresh && !self.busy;
    self.setupApplyButton.enabled=own && !self.busy;
    BOOL hasWeb=[self.snapshot[@"web"][@"installed"] boolValue];
    BOOL webRunning=[self.snapshot[@"web"][@"job"][@"state"] isEqual:@"running"];
    self.setupStartButton.title=hasWeb ? (webRunning ? @"网页服务已启动" : [self.snapshot[@"web"][@"configured"] boolValue] ? @"启动网页服务" : @"打开网页设置…") : @"启动 RDP";
    self.setupStartButton.enabled=!self.busy && !webRunning;
    BOOL ready=running && screen && input && [own[@"capturing"] boolValue];
    self.setupResult.stringValue=ready ? (webRunning ? @"✓ 本账号已就绪。用户可在浏览器打开管理器显示的网页地址。" :
        hasWeb ? @"✓ 本账号已就绪。启动网页服务后即可从浏览器登录。" : @"✓ 本账号已就绪。可使用服务器地址、本地用户名和 Mac 密码连接。") :
        [self.snapshot[@"setup_required"] boolValue] ? @"先启动服务，再按顺序完成本账号的两项授权。" :
        !fresh ? @"正在等待本账号桌面代理。可点击“应用授权并重新检查”启动代理。" :
        screen && input ? @"权限已生效，正在等待桌面采集。" : @"在系统设置中开启 ZMacRDS Desktop；完成两项后，点击下方应用授权。";
    self.setupResult.textColor=ready ? NSColor.systemGreenColor : NSColor.labelColor;
}
- (void)showSetup:(id)sender {
    (void)sender;
    if(!self.setupPanel) {
        self.setupPanel=[[NSPanel alloc] initWithContentRect:NSMakeRect(0,0,760,650)
            styleMask:NSWindowStyleMaskTitled backing:NSBackingStoreBuffered defer:NO];
        self.setupPanel.title=@"首次配置与权限";self.setupPanel.releasedWhenClosed=NO;
        NSStackView *steps=[NSStackView stackViewWithViews:@[]];steps.orientation=NSUserInterfaceLayoutOrientationVertical;
        steps.alignment=NSLayoutAttributeLeading;steps.spacing=12;steps.translatesAutoresizingMaskIntoConstraints=NO;
        [self.setupPanel.contentView addSubview:steps];[NSLayoutConstraint activateConstraints:@[
            [steps.leadingAnchor constraintEqualToAnchor:self.setupPanel.contentView.leadingAnchor constant:24],
            [steps.trailingAnchor constraintEqualToAnchor:self.setupPanel.contentView.trailingAnchor constant:-24],
            [steps.topAnchor constraintEqualToAnchor:self.setupPanel.contentView.topAnchor constant:24]]];
        [steps addArrangedSubview:label([NSString stringWithFormat:@"为 %@ 配置远程桌面",NSUserName()],22,YES)];
        NSTextField *scope=label(@"网络服务由管理员配置一次；下列隐私权限只针对当前登录的 Mac 账号。",12,NO);
        [steps addArrangedSubview:scope];
        [steps addArrangedSubview:label(@"1. 启动服务器",15,YES)];
        self.setupServer=label(@"",13,NO);self.setupServer.maximumNumberOfLines=2;[steps addArrangedSubview:self.setupServer];
        self.setupStartButton=button(@"启动服务",self,@selector(setupStart:));[steps addArrangedSubview:self.setupStartButton];
        [steps addArrangedSubview:label(@"2. 授权本账号的画面、声音和键鼠",15,YES)];
        self.setupScreen=label(@"",13,NO);self.setupInput=label(@"",13,NO);
        self.setupScreenButton=button(@"设置屏幕与系统音频录制…",self,@selector(setupScreenAction:));
        self.setupInputButton=button(@"设置辅助功能／设备控制和数据访问…",self,@selector(setupInputAction:));
        for(NSView *row in @[[NSStackView stackViewWithViews:@[self.setupScreen,self.setupScreenButton]],
                              [NSStackView stackViewWithViews:@[self.setupInput,self.setupInputButton]]])[steps addArrangedSubview:row];
        NSTextField *why=label(@"在系统设置中开启 ZMacRDS Desktop。首次播放声音时，macOS 会另行询问是否允许录制系统音频。键鼠权限在新版系统中称为“设备控制和数据访问”，旧版称为“辅助功能”。",12,NO);
        why.maximumNumberOfLines=3;[steps addArrangedSubview:why];[why.widthAnchor constraintEqualToAnchor:steps.widthAnchor].active=YES;
        [steps addArrangedSubview:label(@"3. 应用授权并检查连接",15,YES)];
        self.setupApplyButton=button(@"应用授权并重新检查",self,@selector(applyPermissions:));[steps addArrangedSubview:self.setupApplyButton];
        self.setupResult=label(@"",13,YES);self.setupResult.maximumNumberOfLines=3;[steps addArrangedSubview:self.setupResult];
        [self.setupResult.widthAnchor constraintEqualToAnchor:steps.widthAnchor].active=YES;
        NSTextField *optional=label(@"可选：若要为尚未登录桌面的用户建立会话，开启系统“屏幕共享”并允许对应账号。已经登录的桌面无需此项。其他用户须在各自账号完成一次上述授权。",12,NO);
        optional.maximumNumberOfLines=3;[steps addArrangedSubview:optional];[optional.widthAnchor constraintEqualToAnchor:steps.widthAnchor].active=YES;
        NSStackView *actions=[NSStackView stackViewWithViews:@[button(@"系统屏幕共享设置…",self,@selector(sharingSettings:)),button(@"本账号连接信息…",self,@selector(ownConnectionInfo:)),button(@"完成",self,@selector(closeSetup:))]];
        actions.spacing=12;[steps addArrangedSubview:actions];
        self.setupMessage=label(@"",12,NO);self.setupMessage.maximumNumberOfLines=2;[steps addArrangedSubview:self.setupMessage];
        [self.setupMessage.widthAnchor constraintEqualToAnchor:steps.widthAnchor].active=YES;
    }
    [self updateSetup];
    if(!self.setupPanel.sheetParent)[self.window beginSheet:self.setupPanel completionHandler:nil];
}
- (void)closeSetup:(id)sender {(void)sender;[self.window endSheet:self.setupPanel];[self.setupPanel orderOut:nil];}
- (void)setupPermission:(NSString *)action {
    if(self.busy)return;
    if(![[self ownDesktop][@"fresh"] boolValue]){self.setupMessage.stringValue=@"请先启动服务及本账号桌面代理。";return;}
    [NSDistributedNotificationCenter.defaultCenter postNotificationName:ZR_PERMISSION_NOTIFICATION
        object:zrPermissionObject() userInfo:@{@"action":action} deliverImmediately:YES];
    self.setupMessage.stringValue=@"系统设置中的应用名称为 ZMacRDS Desktop。完成两项授权后，回到此窗口应用授权。";
}
- (void)setupScreenAction:(id)sender {(void)sender;[self setupPermission:@"screen"];}
- (void)setupInputAction:(id)sender {(void)sender;[self setupPermission:@"input"];}
- (void)applyPermissions:(id)sender {
    (void)sender;NSDictionary *own=[self ownDesktop];if(!own){self.setupMessage.stringValue=@"请先启动服务，等待本地账号识别完成。";return;}
    if([own[@"rdp_connected"] boolValue]) {
        NSAlert *alert=NSAlert.new;alert.messageText=@"重新加载本账号的桌面代理？";
        alert.informativeText=@"本账号的 RDP 连接会断开，桌面应用和其他用户的连接会保留。";
        [alert addButtonWithTitle:@"重新加载"];[alert addButtonWithTitle:@"取消"];
        // The setup sheet is already attached to the main window.
        [alert beginSheetModalForWindow:self.setupPanel completionHandler:^(NSModalResponse result){
            if(result==NSAlertFirstButtonReturn)[self operate:@"restart-agent" uid:@(getuid()) confirmed:YES];
        }];
    } else [self operate:[own[@"job"][@"loaded"] boolValue] ? @"restart-agent" : @"start-agent" uid:@(getuid()) confirmed:YES];
    self.setupMessage.stringValue=@"正在重新加载本账号代理以应用授权；状态会自动更新。";
}
- (void)ownConnectionInfo:(id)sender {
    (void)sender;if(![self ownDesktop]){self.setupMessage.stringValue=@"请先启动服务。";return;}
    [self closeSetup:nil];[self connectionInfoUID:@(getuid()) retried:NO];
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
- (void)updateWeb {
    NSDictionary *web=self.snapshot[@"web"],*job=web[@"job"];
    BOOL installed=[web[@"installed"] boolValue],configured=[web[@"configured"] boolValue];
    BOOL loaded=[job[@"loaded"] boolValue],running=[job[@"state"] isEqual:@"running"];
    BOOL bound=!web[@"address"] || [self.snapshot[@"addresses"] containsObject:web[@"address"]];
    self.webSection.hidden=!installed;
    self.webConfigure.enabled=installed && !self.busy;
    self.webStart.enabled=installed && !running && !self.busy;
    self.webStop.enabled=installed && loaded && !self.busy;
    self.webCopy.enabled=running && [web[@"url"] length]>0;
    self.webExport.hidden=![web[@"quick_tls"] boolValue];
    self.webExport.enabled=!self.busy;
    NSString *status=running ? @"网页服务运行中" : !configured ? @"网页服务尚未配置" :
        loaded ? @"网页服务未就绪" : @"网页服务已停止";
    if(running && web[@"sessions_active"] && [web[@"max_sessions"] unsignedIntegerValue])
        status=[status stringByAppendingFormat:@" · %@/%@ 路在线",web[@"sessions_active"],web[@"max_sessions"]];
    self.webLabel.stringValue=[web[@"url"] length] ? [NSString stringWithFormat:@"%@  ·  %@",status,web[@"url"]] : status;
    self.webLabel.textColor=running ? NSColor.systemGreenColor : NSColor.labelColor;
    self.webHint.stringValue=!bound ? @"服务器地址已改变。打开网页设置，选择当前地址并保存。" :
        !configured ? @"首次启动可自动生成局域网证书。也可在高级设置中导入已有证书。" :
        running && ![web[@"sessions_ready"] boolValue] ? @"网页服务已启动。在线用户状态暂不可用，升级或重启网页服务后可刷新。" :
        loaded && !running ? @"请检查地址、证书有效期和端口。可从“启动网页服务”重试。" :
        [web[@"expires"] length] ? [NSString stringWithFormat:@"用户使用 Mac 用户名和登录密码。证书到期：%@",web[@"expires"]] :
        @"用户使用 Mac 用户名和登录密码。";
    if(installed && self.snapshot[@"lan_port"]==NSNull.null && [self.snapshot[@"broker"][@"state"] isEqual:@"running"])
        self.serverLabel.stringValue=@"本机桌面服务运行中 · 局域网 RDP 未开启";
}
- (void)setupStart:(id)sender {
    if([self.snapshot[@"web"][@"installed"] boolValue]) {
        [self closeSetup:nil];[self startWeb:sender];
    } else [self startServer:sender];
}
- (void)copyWeb:(id)sender {
    (void)sender;NSString *url=self.snapshot[@"web"][@"url"];if(!url.length)return;
    [NSPasteboard.generalPasteboard clearContents];[NSPasteboard.generalPasteboard setString:url forType:NSPasteboardTypeString];
    self.message.stringValue=@"已复制 HTTPS 地址";
}
- (void)runWeb:(NSString *)action settings:(NSDictionary *)settings retried:(BOOL)retried {
    if(self.busy)return;
    if(!self.authorizationForm && ![self authenticate]) {
        self.webFormMessage.stringValue=self.message.stringValue;return;
    }
    self.busy=YES;[self updateWeb];
    NSTextField *progress=self.webPanel.sheetParent ? self.webFormMessage : self.message;
    progress.stringValue=@"正在处理网页服务…";
    NSMutableDictionary *request=[self request:action uid:nil];if(settings)[request addEntriesFromDictionary:settings];
    dispatch_async(dispatch_get_global_queue(QOS_CLASS_USER_INITIATED,0),^{
        NSDictionary *reply=zrRequest(request);dispatch_async(dispatch_get_main_queue(),^{
            self.busy=NO;
            if(!retried && [reply[@"error"] isEqual:@"authorization_required"]){
                [self lockAuthorization];
                [self runWeb:action settings:settings retried:YES];return;
            }
            BOOL ok=[reply[@"ok"] boolValue];NSString *message=reply[@"message"] ?: @"网页服务操作未完成";
            if(ok && [action isEqual:@"export-web"]){
                NSData *certificate=[[NSData alloc] initWithBase64EncodedString:reply[@"certificate_der"] ?: @"" options:0];
                NSSavePanel *save=NSSavePanel.savePanel;save.nameFieldStringValue=@"ZMacRDS-LAN.cer";
                save.message=@"将公开证书传给要连接的 Mac 或 Windows，并在客户端设置信任。";
                [save beginSheetModalForWindow:self.window completionHandler:^(NSModalResponse result){
                    if(result==NSModalResponseOK)
                        self.message.stringValue=[certificate writeToURL:save.URL atomically:YES] ? @"已导出局域网证书" : @"证书未保存";
                }];[self updateWeb];return;
            }
            if(ok && self.webPanel.sheetParent){[self closeWebSettings:nil];self.setupAutoPresented=NO;}
            if(!ok && self.webPanel.sheetParent)self.webFormMessage.stringValue=message;
            self.message.stringValue=message;[self refreshAction:nil];
        });
    });
}
- (void)startWeb:(id)sender {
    if(self.busy)return;
    NSDictionary *web=self.snapshot[@"web"];
    if(![web[@"configured"] boolValue] || (web[@"address"] && ![self.snapshot[@"addresses"] containsObject:web[@"address"]])){
        [self showWebSettings:sender];return;
    }
    [self runWeb:@"start-web" settings:nil retried:NO];
}
- (void)exportWeb:(id)sender {
    (void)sender;[self runWeb:@"export-web" settings:nil retried:NO];
}
- (void)stopWeb:(id)sender {
    (void)sender;if(self.busy)return;
    NSAlert *alert=NSAlert.new;alert.messageText=@"停止网页服务？";
    alert.informativeText=@"当前浏览器连接会断开。用户的 Mac 桌面和打开的应用会保留。网页服务会保持停止，直到你重新启动。";
    [alert addButtonWithTitle:@"停止"];[alert addButtonWithTitle:@"取消"];
    [alert beginSheetModalForWindow:self.window completionHandler:^(NSModalResponse result){
        if(result==NSAlertFirstButtonReturn)[self runWeb:@"stop-web" settings:nil retried:NO];
    }];
}
- (void)selectWebAddress:(id)sender {
    (void)sender;self.webSubnet.stringValue=self.webAddress.selectedItem.representedObject[@"subnet"] ?: @"";
}
- (void)toggleWebAdvanced:(id)sender {
    (void)sender;self.webAdvancedFields.hidden=self.webAdvanced.state!=NSControlStateValueOn;
}
- (void)showWebSettings:(id)sender {
    (void)sender;if(self.busy || self.webPanel.sheetParent)return;
    self.webCertificate=self.webPrivateKey=nil;
    NSDictionary *current=self.snapshot[@"web"];
    NSArray *networks=self.snapshot[@"networks"] ?: @[];
    self.webPanel=[[NSPanel alloc] initWithContentRect:NSMakeRect(0,0,620,480) styleMask:NSWindowStyleMaskTitled backing:NSBackingStoreBuffered defer:NO];
    self.webPanel.title=@"网页设置";self.webPanel.releasedWhenClosed=NO;
    NSStackView *form=[NSStackView stackViewWithViews:@[]];form.orientation=NSUserInterfaceLayoutOrientationVertical;
    form.alignment=NSLayoutAttributeLeading;form.spacing=12;form.translatesAutoresizingMaskIntoConstraints=NO;
    NSView *view=self.webPanel.contentView;[view addSubview:form];
    [NSLayoutConstraint activateConstraints:@[[form.leadingAnchor constraintEqualToAnchor:view.leadingAnchor constant:22],
        [form.trailingAnchor constraintEqualToAnchor:view.trailingAnchor constant:-22],
        [form.topAnchor constraintEqualToAnchor:view.topAnchor constant:22]]];
    [form addArrangedSubview:label(@"局域网 HTTPS",21,YES)];
    NSTextField *intro=label(@"确认局域网地址后即可启动。自动生成的证书可从主窗口导出，客户端首次连接前导入一次。",13,NO);
    intro.maximumNumberOfLines=3;[form addArrangedSubview:intro];[intro.widthAnchor constraintEqualToAnchor:form.widthAnchor].active=YES;
    self.webAddress=[[NSPopUpButton alloc] initWithFrame:NSZeroRect pullsDown:NO];
    self.webAddress.target=self;self.webAddress.action=@selector(selectWebAddress:);
    for(NSDictionary *network in networks) {
        [self.webAddress addItemWithTitle:[NSString stringWithFormat:@"%@ (%@)",network[@"address"],network[@"interface"]]];
        self.webAddress.lastItem.representedObject=network;
        if([network[@"address"] isEqual:current[@"address"]])[self.webAddress selectItem:self.webAddress.lastItem];
    }
    self.webAddress.enabled=networks.count>0;
    NSDictionary *selected=self.webAddress.selectedItem.representedObject;
    NSString *subnet=[selected[@"address"] isEqual:current[@"address"]] ? current[@"subnet"] : nil;
    self.webSubnet=[NSTextField textFieldWithString:subnet ?: selected[@"subnet"] ?: @""];
    self.webHost=[NSTextField textFieldWithString:[current[@"hostname"] isEqual:current[@"address"]] ? @"" : current[@"hostname"] ?: @""];
    self.webHost.placeholderString=@"例如 desktop.example.com；使用 IP 证书时留空";
    self.webPort=[NSTextField textFieldWithString:[current[@"port"] stringValue] ?: @"8443"];
    self.webMaxSessions=[NSTextField textFieldWithString:[current[@"max_sessions"] stringValue] ?: @"5"];
    NSArray *fields=@[@[@"服务器 IPv4",self.webAddress],@[@"允许访问的网段",self.webSubnet],@[@"HTTPS 端口",self.webPort],@[@"同时连接人数",self.webMaxSessions]];
    for(NSArray *entry in fields) {
        NSTextField *caption=label(entry[0],13,YES);NSControl *input=entry[1];
        [caption setAccessibilityRole:NSAccessibilityStaticTextRole];[input setAccessibilityLabel:entry[0]];
        NSStackView *row=[NSStackView stackViewWithViews:@[caption,input]];row.spacing=10;
        [form addArrangedSubview:row];
        [caption.widthAnchor constraintEqualToConstant:133].active=YES;
        [input.widthAnchor constraintEqualToConstant:387].active=YES;
    }
    self.webCertButton=button(@"选择证书链…",self,@selector(pickCertificate:));
    self.webKeyButton=button(@"选择私钥…",self,@selector(pickPrivateKey:));
    self.webAdvanced=[NSButton checkboxWithTitle:@"使用已有 HTTPS 证书" target:self action:@selector(toggleWebAdvanced:)];
    self.webAdvanced.state=[current[@"configured"] boolValue] && ![current[@"quick_tls"] boolValue] ? NSControlStateValueOn : NSControlStateValueOff;
    [form addArrangedSubview:self.webAdvanced];
    [self.webHost setAccessibilityLabel:@"HTTPS 域名（可选）"];
    NSStackView *hostRow=[NSStackView stackViewWithViews:@[label(@"HTTPS 域名（可选）",13,YES),self.webHost]];
    [self.webHost.widthAnchor constraintEqualToConstant:387].active=YES;
    NSStackView *files=[NSStackView stackViewWithViews:@[self.webCertButton,self.webKeyButton]];files.spacing=10;
    self.webAdvancedFields=[NSStackView stackViewWithViews:@[hostRow,files]];
    self.webAdvancedFields.orientation=NSUserInterfaceLayoutOrientationVertical;
    self.webAdvancedFields.alignment=NSLayoutAttributeLeading;
    [form addArrangedSubview:self.webAdvancedFields];[self toggleWebAdvanced:nil];
    NSTextField *note=label(@"网页服务仅开放给所选网段。启动后，局域网 RDP 入口会停止。",12,NO);
    note.maximumNumberOfLines=2;[form addArrangedSubview:note];[note.widthAnchor constraintEqualToAnchor:form.widthAnchor].active=YES;
    self.webFormMessage=label(networks.count ? @"" : @"当前没有可用的私有 IPv4 地址。请连接局域网后重新打开此窗口。",12,NO);
    self.webFormMessage.maximumNumberOfLines=3;[form addArrangedSubview:self.webFormMessage];
    [self.webFormMessage.widthAnchor constraintEqualToAnchor:form.widthAnchor].active=YES;
    NSButton *save=button(@"保存并启动",self,@selector(saveWebSettings:));save.enabled=networks.count>0;
    NSStackView *actions=[NSStackView stackViewWithViews:@[save,button(@"取消",self,@selector(closeWebSettings:))]];actions.spacing=10;[form addArrangedSubview:actions];
    [self.window beginSheet:self.webPanel completionHandler:nil];
}
- (void)closeWebSettings:(id)sender {
    (void)sender;if(self.busy)return;
    [self.window endSheet:self.webPanel];[self.webPanel orderOut:nil];
    self.webCertificate=self.webPrivateKey=nil;
}
- (void)pickWebFile:(BOOL)certificate {
    if(self.busy)return;
    NSOpenPanel *panel=NSOpenPanel.openPanel;panel.canChooseDirectories=NO;panel.allowsMultipleSelection=NO;
    panel.title=certificate ? @"选择 HTTPS 证书链" : @"选择对应的 HTTPS 私钥";
    [panel beginSheetModalForWindow:self.webPanel completionHandler:^(NSModalResponse response){
        if(response!=NSModalResponseOK)return;
        if(certificate){self.webCertificate=panel.URL;self.webCertButton.title=@"证书链已选择";}
        else{self.webPrivateKey=panel.URL;self.webKeyButton.title=@"私钥已选择";}
        self.webFormMessage.stringValue=@"";
    }];
}
- (void)pickCertificate:(id)sender {(void)sender;[self pickWebFile:YES];}
- (void)pickPrivateKey:(id)sender {(void)sender;[self pickWebFile:NO];}
- (void)saveWebSettings:(id)sender {
    (void)sender;if(self.busy)return;
    NSString *port=self.webPort.stringValue;NSInteger number=port.integerValue;
    if(!port.length || [port rangeOfCharacterFromSet:NSCharacterSet.decimalDigitCharacterSet.invertedSet].location!=NSNotFound ||
       number<1024 || number>65535 || number==3390){self.webFormMessage.stringValue=@"请输入 1024–65535 的 HTTPS 端口，3390 为保留端口。";return;}
    NSString *capacity=self.webMaxSessions.stringValue;NSInteger maximum=capacity.integerValue;
    if([capacity rangeOfCharacterFromSet:NSCharacterSet.decimalDigitCharacterSet.invertedSet].location!=NSNotFound ||
       maximum<1 || maximum>64){self.webFormMessage.stringValue=@"同时连接人数须为 1–64。";return;}
    NSString *address=self.webAddress.selectedItem.representedObject[@"address"];
    if(!address.length){self.webFormMessage.stringValue=@"请选择此 Mac 的局域网地址。";return;}
    NSString *(^trim)(NSString *)=^NSString *(NSString *s){return [s stringByTrimmingCharactersInSet:NSCharacterSet.whitespaceAndNewlineCharacterSet];};
    if(self.webAdvanced.state!=NSControlStateValueOn){
        [self runWeb:@"quick-web" settings:@{@"address":address,@"subnet":trim(self.webSubnet.stringValue),@"port":@(number),@"max_sessions":@(maximum)} retried:NO];return;
    }
    NSData *certificate=readPEM(self.webCertificate,24576),*key=readPEM(self.webPrivateKey,8192);
    if(!certificate || !key){self.webFormMessage.stringValue=@"请选择可读取的 PEM 证书链和对应私钥。";return;}
    [self runWeb:@"configure-web" settings:@{@"address":address,@"subnet":trim(self.webSubnet.stringValue),
        @"hostname":trim(self.webHost.stringValue),@"port":@(number),@"max_sessions":@(maximum),@"certificate":[certificate base64EncodedStringWithOptions:0],
        @"private_key":[key base64EncodedStringWithOptions:0]} retried:NO];
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
            NSString *certificate=[reply[@"certificate_pem"] isKindOfClass:NSString.class] ? reply[@"certificate_pem"] : nil;
            if(certificate.length)[alert addButtonWithTitle:@"导出服务器证书…"];
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
                if(response==NSAlertThirdButtonReturn+1 && certificate.length) {
                    NSSavePanel *panel=NSSavePanel.savePanel;panel.nameFieldStringValue=@"ZMacRDS-server.pem";
                    [panel beginSheetModalForWindow:self.window completionHandler:^(NSModalResponse result){
                        if(result!=NSModalResponseOK)return;
                        if(![certificate writeToURL:panel.URL atomically:YES encoding:NSUTF8StringEncoding error:NULL])self.message.stringValue=@"证书未保存，请检查目标文件夹。";
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
