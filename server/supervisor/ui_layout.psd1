# Click targets for the host-game menus, in the game's 800x600 UI space.
# Every point is the centre of the control's `rect` in the retail interface
# files (Logic.dsres: ui/interfaces/...). Combo lists use the listbox top and
# its element height of 16 (oncreated = setelementheight(16)).
@{
    MainMenu = @{
        Multiplayer       = @(398, 229)   # frontend/main_menu: button_multi_player
    }
    Provider = @{
        Internet          = @(401, 292)   # frontend/multiplayer_provider: button_internet
    }
    InternetMenu = @{
        PlayerName        = @(400, 102)   # internet_game_menu: internet_player_name_edit_box
        Host              = @(575, 214)   # internet_game_menu: button_host
    }
    HostDialog = @{
        GameName          = @(400, 255)   # host_game: game_name_edit_box
        PasswordCheck     = @(258, 290)   # host_game: checkbox_password_game
        Password          = @(450, 289)   # host_game: game_password_edit_box
        Ok                = @(325, 352)   # host_game: button_ok
    }
    Staging = @{
        SelectCharacter   = @(651, 110)   # staging_area_players: button_select_char1 (host is row 1)
        MapSettings       = @(526, 42)    # staging_area: button_map_settings
        GameSettings      = @(696, 42)    # staging_area: button_game_settings
        Ready             = @(592, 506)   # staging_area: staging_checkbox_ready
        Start             = @(668, 506)   # staging_area: staging_area_button_start_game
    }
    SelectCharacter = @{
        FirstRow          = @(390, 150)   # staging_area_select_character: character1_sel
        RowStep           = 40            # character rows are 40 px apart
        Create            = @(446, 84)    # staging_character_create
        Accept            = @(322, 316)   # staging_character_ok
    }
    CreateCharacter = @{
        Name              = @(147, 200)   # staging_area_create_character: staging_char_name_edit_box
        Accept            = @(322, 316)   # staging_create_character_ok
    }
    MapSettings = @{
        MapListTop        = 103           # staging_map_list rect top; x centre 350
        MapListX          = 350
        WorldCombo        = @(670, 110)   # combo_map_world_button
        WorldListTop      = 120           # listbox_map_world; x centre 590
        WorldListX        = 590
        Accept            = @(322, 316)   # staging_button_accept_map
    }
    GameSettings = @{
        PlayerLimitCombo  = @(240, 128)   # combo_player_limit_button
        PlayerLimitTop    = 138           # listbox_player_limit
        DifficultyCombo   = @(240, 178)   # combo_difficulty_button
        DifficultyTop     = 188           # listbox_difficulty
        DropCombo         = @(240, 278)   # combo_characters_drop_button
        DropTop           = 288           # listbox_characters_drop
        ListX             = 165
        Accept            = @(322, 316)   # staging_button_accept_game
    }
    ElementHeight = 16
}
