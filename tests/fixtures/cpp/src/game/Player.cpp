#include "Player.h"
#include "Common.h"
#include "zlib.h"

namespace Game {

Player::~Player()
{
    m_session = nullptr;
}

// Regenerates health, then ticks spell timers.
void Player::Update(uint32 diff)
{
    m_health += static_cast<int>(diff / kTickMs);
}

const Session& Game::Player::GetSession() const { return *m_session; }

static bool operator==(const Player& a, const Player& b) { return &a == &b; }

} // namespace Game
